"""Yandex SpeechKit Speech-to-Text plugin.

- Batch recognition: REST `POST /speech/v1/stt:recognize` (1 MB / 30 s per call).
- Streaming: bidirectional gRPC `Recognizer.RecognizeStreaming` (v3).

The streaming impl delegates end-of-utterance to LiveKit (Silero VAD) via the
`ExternalEouClassifier` and sends `Eou()` from the caller side; this avoids
conflicts between two EOU detectors. Yandex enforces a 5 min per-session cap —
we transparently reconnect on `FINAL_TRANSCRIPT` boundaries.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from dataclasses import dataclass, field
from typing import AsyncGenerator

import aiohttp
import grpc
import grpc.aio
from livekit import rtc

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    stt,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer
from livekit.agents.utils.misc import is_given

from ._auth import YandexCredentials, resolve_credentials
from ._proto.yandex.cloud.ai.stt.v3 import stt_pb2, stt_service_pb2_grpc
from ._retry import compute_backoff, raise_from_grpc, with_retry
from .log import logger
from .models import (
    DEFAULT_SAMPLE_RATE,
    STT_GRPC_ENDPOINT,
    STT_REST_BASE,
    STT_SESSION_MAX_DURATION_S,
    STTLanguage,
    STTModel,
)

# Reconnect ~15 s before the server-side 5-min cap to avoid getting cut
# mid-utterance.
_STT_RECONNECT_SAFETY_MARGIN_S = 15
_STT_MAX_SESSION_S = STT_SESSION_MAX_DURATION_S - _STT_RECONNECT_SAFETY_MARGIN_S

# gRPC codes that should trigger a transparent reconnect with backoff rather
# than surfacing to the caller.
_RETRIABLE_GRPC_CODES: frozenset[grpc.StatusCode] = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
)


@dataclass
class _STTOptions:
    model: str
    language: str
    sample_rate: int
    profanity_filter: bool
    raw_results: bool
    rest_endpoint: str
    grpc_endpoint: str
    # Streaming-only options.
    interim_results: bool
    language_hints: list[str] = field(default_factory=list)
    language_restriction: str = "whitelist"  # "whitelist" | "blacklist" | "none"
    external_eou: bool = True
    audio_processing: str = "real_time"  # "real_time" | "full_data"


class STT(stt.STT):
    """Yandex SpeechKit STT.

    Supports both batch (REST) and streaming (bidirectional gRPC) recognition.
    LiveKit will prefer `stream()` automatically when `capabilities.streaming` is True.

    Multilingual modes:
      * `language="ru-RU"` — single language
      * `language="auto"` + `language_hints=["ru-RU", "en-US"]` — auto-detect from whitelist
      * `language="auto"` — unrestricted auto-detect
    """

    def __init__(
        self,
        *,
        model: STTModel | str = "general",
        language: STTLanguage | str = "ru-RU",
        language_hints: list[str] | None = None,
        language_restriction: str = "whitelist",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        profanity_filter: bool = False,
        raw_results: bool = False,
        interim_results: bool = True,
        external_eou: bool = True,
        rest_endpoint: str = STT_REST_BASE,
        grpc_endpoint: str = STT_GRPC_ENDPOINT,
        http_session: aiohttp.ClientSession | None = None,
        api_key: str | None = None,
        folder_id: str | None = None,
        iam_token: str | None = None,
        oauth_token: str | None = None,
        credentials: YandexCredentials | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=interim_results,
            )
        )
        self._credentials = credentials or resolve_credentials(
            api_key=api_key,
            iam_token=iam_token,
            oauth_token=oauth_token,
            folder_id=folder_id,
            http_session=http_session,
        )
        if not self._credentials.folder_id:
            logger.warning(
                "YANDEX_FOLDER_ID is not set; Yandex STT /speech/v1/stt:recognize requires folderId."
            )
        if language_restriction not in ("whitelist", "blacklist", "none"):
            raise ValueError("language_restriction must be 'whitelist', 'blacklist' or 'none'")
        self._opts = _STTOptions(
            model=model,
            language=language,
            sample_rate=sample_rate,
            profanity_filter=profanity_filter,
            raw_results=raw_results,
            rest_endpoint=rest_endpoint,
            grpc_endpoint=grpc_endpoint,
            interim_results=interim_results,
            language_hints=list(language_hints or []),
            language_restriction=language_restriction,
            external_eou=external_eou,
        )
        self._session = http_session
        self._channel: grpc.aio.Channel | None = None
        self._channel_lock = asyncio.Lock()
        self._streams = weakref.WeakSet["SpeechStream"]()
        self._reconnect_count: int = 0

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    async def _ensure_channel(self) -> grpc.aio.Channel:
        async with self._channel_lock:
            if self._channel is None:
                self._channel = grpc.aio.secure_channel(
                    self._opts.grpc_endpoint, grpc.ssl_channel_credentials()
                )
            return self._channel

    def update_options(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        language_hints: list[str] | None = None,
        language_restriction: str | None = None,
        profanity_filter: bool | None = None,
        interim_results: bool | None = None,
    ) -> None:
        if model is not None:
            self._opts.model = model
        if language is not None:
            self._opts.language = language
        if language_hints is not None:
            self._opts.language_hints = list(language_hints)
        if language_restriction is not None:
            if language_restriction not in ("whitelist", "blacklist", "none"):
                raise ValueError(
                    "language_restriction must be 'whitelist', 'blacklist' or 'none'"
                )
            self._opts.language_restriction = language_restriction
        if profanity_filter is not None:
            self._opts.profanity_filter = profanity_filter
        if interim_results is not None:
            self._opts.interim_results = interim_results
        for s in list(self._streams):
            s._reconnect_event.set()

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        lang = language if is_given(language) else self._opts.language
        frame = utils.merge_frames(buffer)
        pcm_bytes = bytes(frame.data)

        params = {
            "format": "lpcm",
            "sampleRateHertz": str(self._opts.sample_rate),
            "lang": lang,
            "model": self._opts.model,
            "profanityFilter": "true" if self._opts.profanity_filter else "false",
            "rawResults": "true" if self._opts.raw_results else "false",
        }
        if self._credentials.folder_id:
            params["folderId"] = self._credentials.folder_id

        url = f"{self._opts.rest_endpoint}/speech/v1/stt:recognize"
        session = self._ensure_session()

        async def _do_post() -> dict[str, object]:
            headers = await self._credentials.http_headers()
            async with session.post(
                url,
                params=params,
                headers=headers,
                data=pcm_bytes,
                timeout=aiohttp.ClientTimeout(total=conn_options.timeout),
            ) as res:
                body = await res.text()
                if res.status != 200:
                    # aiohttp does not raise on non-2xx by default; we want
                    # ClientResponseError so the retry layer can decide.
                    raise aiohttp.ClientResponseError(
                        res.request_info,
                        res.history,
                        status=res.status,
                        message=body,
                        headers=res.headers,
                    )
                return await res.json()

        started = time.monotonic()
        try:
            payload = await with_retry(
                "STT.recognize", _do_post, conn_options=conn_options
            )
        finally:
            logger.debug(
                "Yandex STT recognize completed in %.1f ms",
                (time.monotonic() - started) * 1000,
            )

        text = (payload.get("result") or "").strip() if isinstance(payload, dict) else ""
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang, text=text, confidence=1.0)],
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "SpeechStream":
        effective_lang = language if is_given(language) else self._opts.language
        s = SpeechStream(stt=self, conn_options=conn_options, language=effective_lang)
        self._streams.add(s)
        return s

    @property
    def reconnect_count(self) -> int:
        """Total number of streaming session reconnects across all streams."""
        return self._reconnect_count

    async def aclose(self) -> None:
        for s in list(self._streams):
            await s.aclose()
        self._streams.clear()
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        await self._credentials.aclose()


class SpeechStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: STT,
        conn_options: APIConnectOptions,
        language: str,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=stt._opts.sample_rate)
        self._stt: STT = stt
        self._language = language
        self._reconnect_event = asyncio.Event()
        self._session_started_at: float = 0.0
        self._has_started = False

    def _build_language_restriction(self) -> stt_pb2.LanguageRestrictionOptions | None:
        lang_restriction = self._stt._opts.language_restriction
        codes: list[str]
        if self._language == "auto":
            codes = list(self._stt._opts.language_hints)
            if not codes:
                return None
            restriction_type = stt_pb2.LanguageRestrictionOptions.WHITELIST
        else:
            codes = [self._language]
            restriction_type = (
                stt_pb2.LanguageRestrictionOptions.WHITELIST
                if lang_restriction == "whitelist"
                else stt_pb2.LanguageRestrictionOptions.BLACKLIST
            )
        if lang_restriction == "none":
            return None
        return stt_pb2.LanguageRestrictionOptions(
            restriction_type=restriction_type,
            language_code=codes,
        )

    def _build_session_options_request(self) -> stt_pb2.StreamingRequest:
        raw_audio = stt_pb2.RawAudio(
            audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
            sample_rate_hertz=self._stt._opts.sample_rate,
            audio_channel_count=1,
        )
        audio_format = stt_pb2.AudioFormatOptions(raw_audio=raw_audio)

        audio_processing = (
            stt_pb2.RecognitionModelOptions.REAL_TIME
            if self._stt._opts.audio_processing == "real_time"
            else stt_pb2.RecognitionModelOptions.FULL_DATA
        )

        recognition_model = stt_pb2.RecognitionModelOptions(
            model=self._stt._opts.model,
            audio_format=audio_format,
            audio_processing_type=audio_processing,
        )
        restriction = self._build_language_restriction()
        if restriction is not None:
            recognition_model.language_restriction.CopyFrom(restriction)

        eou_classifier: stt_pb2.EouClassifierOptions | None = None
        if self._stt._opts.external_eou:
            eou_classifier = stt_pb2.EouClassifierOptions(
                external_classifier=stt_pb2.ExternalEouClassifier()
            )
        else:
            eou_classifier = stt_pb2.EouClassifierOptions(
                default_classifier=stt_pb2.DefaultEouClassifier()
            )

        session_options = stt_pb2.StreamingOptions(
            recognition_model=recognition_model,
            eou_classifier=eou_classifier,
        )
        return stt_pb2.StreamingRequest(session_options=session_options)

    @staticmethod
    def _pick_language(alt: stt_pb2.Alternative, fallback: str) -> str:
        if alt.languages:
            best = max(alt.languages, key=lambda le: le.probability)
            if best.language_code:
                return best.language_code
        return fallback

    async def _run(self) -> None:
        attempt = 0
        while True:
            self._reconnect_event.clear()
            try:
                await self._run_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except grpc.aio.AioRpcError as e:
                if e.code() not in _RETRIABLE_GRPC_CODES:
                    raise_from_grpc(e, action="STT.RecognizeStreaming")
                attempt += 1
                delay = compute_backoff(
                    attempt, self._conn_options.retry_interval
                )
                self._stt._reconnect_count += 1
                logger.info(
                    "Yandex STT stream transient error %s (attempt %d); reconnecting in %.2fs",
                    e.code().name if e.code() else "UNKNOWN",
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)
            except Exception:
                if not self._reconnect_event.is_set():
                    raise
                self._stt._reconnect_count += 1
                logger.debug("Yandex STT stream reconnecting (explicit)")

    async def _run_once(self) -> None:
        self._session_started_at = time.time()

        channel = await self._stt._ensure_channel()
        stub = stt_service_pb2_grpc.RecognizerStub(channel)
        metadata = await self._stt._credentials.grpc_metadata()

        # audio_input_ch buffers frames destined for the gRPC send side; it lets us
        # close the send side cleanly on reconnect without losing pending frames.
        audio_input_ch: utils.aio.Chan[rtc.AudioFrame | object] = utils.aio.Chan()
        stop_send = object()

        async def input_generator() -> AsyncGenerator[stt_pb2.StreamingRequest, None]:
            yield self._build_session_options_request()
            try:
                while True:
                    item = await audio_input_ch.recv()
                    if item is stop_send:
                        return
                    if isinstance(item, rtc.AudioFrame):
                        yield stt_pb2.StreamingRequest(
                            chunk=stt_pb2.AudioChunk(data=bytes(item.data))
                        )
            except utils.aio.ChanClosed:
                return

        async def feed_audio() -> None:
            try:
                async for frame in self._input_ch:
                    if isinstance(frame, rtc.AudioFrame):
                        if not audio_input_ch.closed:
                            audio_input_ch.send_nowait(frame)
                    if time.time() - self._session_started_at > _STT_MAX_SESSION_S:
                        logger.debug(
                            "Yandex STT approaching 5-minute session cap, reconnecting"
                        )
                        self._reconnect_event.set()
                        break
            finally:
                if not audio_input_ch.closed:
                    audio_input_ch.send_nowait(stop_send)
                    audio_input_ch.close()

        async def process_responses() -> None:
            call = stub.RecognizeStreaming(input_generator(), metadata=metadata)
            async for response in call:
                await self._handle_response(response)
                if self._reconnect_event.is_set():
                    break

        feed_task = asyncio.create_task(feed_audio(), name="yandex-stt-feed")
        try:
            await process_responses()
        finally:
            self._reconnect_event.set()
            if not audio_input_ch.closed:
                try:
                    audio_input_ch.send_nowait(stop_send)
                except utils.aio.ChanClosed:
                    pass
                audio_input_ch.close()
            await utils.aio.cancel_and_wait(feed_task)

    async def _handle_response(self, response: stt_pb2.StreamingResponse) -> None:
        event_kind = response.WhichOneof("Event")
        if event_kind == "partial":
            alts = self._alternatives_from_update(response.partial)
            if alts:
                if not self._has_started:
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                    )
                    self._has_started = True
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.INTERIM_TRANSCRIPT, alternatives=alts
                    )
                )
        elif event_kind == "final":
            alts = self._alternatives_from_update(response.final)
            if alts:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=alts
                    )
                )
        elif event_kind == "eou_update":
            if self._has_started:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                )
                self._has_started = False
        elif event_kind == "status_code":
            # Keep-alive from the server; ignore.
            return

    def _alternatives_from_update(
        self, update: stt_pb2.AlternativeUpdate
    ) -> list[stt.SpeechData]:
        result: list[stt.SpeechData] = []
        for alt in update.alternatives:
            if not alt.text:
                continue
            result.append(
                stt.SpeechData(
                    language=self._pick_language(alt, self._language),
                    text=alt.text,
                    start_time=alt.start_time_ms / 1000.0,
                    end_time=alt.end_time_ms / 1000.0,
                    confidence=alt.confidence or 1.0,
                )
            )
        return result

