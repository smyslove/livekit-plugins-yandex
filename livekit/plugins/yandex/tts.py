"""Yandex SpeechKit v3 Text-to-Speech plugin.

- Batch synthesis: `Synthesizer.UtteranceSynthesis` (server-streaming gRPC).
  250-char limit per request unless `unsafe_mode=True` (billed per 250 chars).
- Streaming synthesis: `Synthesizer.StreamSynthesis` (bidirectional gRPC).
  Text is split into sentences by a tokenizer and each sentence is sent as a
  separate `SynthesisInput`; audio chunks are pushed back as soon as the server
  produces them.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from dataclasses import dataclass, replace
from typing import AsyncGenerator

import grpc
import grpc.aio

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    APITimeoutError,
    tokenize,
    tts,
    utils,
)

from ._auth import YandexCredentials, resolve_credentials
from ._proto.yandex.cloud.ai.tts.v3 import tts_pb2, tts_service_pb2_grpc
from ._retry import raise_from_grpc, with_retry
from .log import logger
from .models import (
    DEFAULT_NUM_CHANNELS,
    DEFAULT_SAMPLE_RATE,
    TTS_GRPC_ENDPOINT,
    TTS_MAX_CHARS_PER_REQUEST,
    TTSVoice,
)


@dataclass
class _TTSOptions:
    voice: str
    role: str | None
    speed: float
    volume: float
    pitch_shift: float
    sample_rate: int
    num_channels: int
    model: str
    endpoint: str
    unsafe_mode: bool
    tokenizer: tokenize.SentenceTokenizer


class TTS(tts.TTS):
    """Yandex SpeechKit TTS.

    Usage:
        ```python
        from livekit.plugins import yandex
        tts = yandex.TTS(voice="alena", speed=1.0, sample_rate=8000)
        ```

    Credentials are resolved in this order:
      1. `api_key` / `folder_id` kwargs
      2. `YANDEX_API_KEY` / `YANDEX_FOLDER_ID` env vars
    """

    def __init__(
        self,
        *,
        voice: TTSVoice | str = "alena",
        role: str | None = None,
        speed: float = 1.0,
        volume: float = 0.0,
        pitch_shift: float = 0.0,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        num_channels: int = DEFAULT_NUM_CHANNELS,
        model: str = "",
        unsafe_mode: bool = False,
        endpoint: str = TTS_GRPC_ENDPOINT,
        tokenizer: tokenize.SentenceTokenizer | None = None,
        api_key: str | None = None,
        folder_id: str | None = None,
        iam_token: str | None = None,
        oauth_token: str | None = None,
        credentials: YandexCredentials | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=num_channels,
        )

        if not 0.1 <= speed <= 3.0:
            raise ValueError("speed must be between 0.1 and 3.0")
        if not -1000.0 <= pitch_shift <= 1000.0:
            raise ValueError("pitch_shift must be between -1000 and 1000 Hz")

        self._credentials = credentials or resolve_credentials(
            api_key=api_key,
            iam_token=iam_token,
            oauth_token=oauth_token,
            folder_id=folder_id,
        )
        self._synthesized_chars: int = 0
        self._opts = _TTSOptions(
            voice=voice,
            role=role,
            speed=speed,
            volume=volume,
            pitch_shift=pitch_shift,
            sample_rate=sample_rate,
            num_channels=num_channels,
            model=model,
            endpoint=endpoint,
            unsafe_mode=unsafe_mode,
            tokenizer=tokenizer or tokenize.basic.SentenceTokenizer(),
        )
        self._channel: grpc.aio.Channel | None = None
        self._channel_lock = asyncio.Lock()
        self._streams = weakref.WeakSet["SynthesizeStream"]()

    @property
    def model(self) -> str:
        return self._opts.model or "yandex-tts-v3"

    @property
    def provider(self) -> str:
        return "yandex"

    def update_options(
        self,
        *,
        voice: str | None = None,
        role: str | None = None,
        speed: float | None = None,
        volume: float | None = None,
        pitch_shift: float | None = None,
        unsafe_mode: bool | None = None,
    ) -> None:
        if voice is not None:
            self._opts.voice = voice
        if role is not None:
            self._opts.role = role
        if speed is not None:
            if not 0.1 <= speed <= 3.0:
                raise ValueError("speed must be between 0.1 and 3.0")
            self._opts.speed = speed
        if volume is not None:
            self._opts.volume = volume
        if pitch_shift is not None:
            self._opts.pitch_shift = pitch_shift
        if unsafe_mode is not None:
            self._opts.unsafe_mode = unsafe_mode

    async def _ensure_channel(self) -> grpc.aio.Channel:
        async with self._channel_lock:
            if self._channel is None:
                self._channel = grpc.aio.secure_channel(
                    self._opts.endpoint, grpc.ssl_channel_credentials()
                )
            return self._channel

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions | None = None,
    ) -> tts.ChunkedStream:
        if conn_options is None:
            conn_options = DEFAULT_API_CONNECT_OPTIONS
        return _ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "SynthesizeStream":
        s = SynthesizeStream(tts=self, conn_options=conn_options)
        self._streams.add(s)
        return s

    @property
    def synthesized_chars(self) -> int:
        """Total characters submitted for synthesis (used for billing reconciliation)."""
        return self._synthesized_chars

    async def aclose(self) -> None:
        for s in list(self._streams):
            await s.aclose()
        self._streams.clear()
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        await self._credentials.aclose()


def _build_audio_format(sample_rate: int) -> tts_pb2.AudioFormatOptions:
    raw = tts_pb2.RawAudio(
        audio_encoding=tts_pb2.RawAudio.LINEAR16_PCM,
        sample_rate_hertz=sample_rate,
    )
    return tts_pb2.AudioFormatOptions(raw_audio=raw)


def _build_hints(opts: _TTSOptions) -> list[tts_pb2.Hints]:
    hints: list[tts_pb2.Hints] = [
        tts_pb2.Hints(voice=opts.voice),
        tts_pb2.Hints(speed=opts.speed),
    ]
    if opts.role:
        hints.append(tts_pb2.Hints(role=opts.role))
    if opts.volume != 0.0:
        hints.append(tts_pb2.Hints(volume=opts.volume))
    if opts.pitch_shift != 0.0:
        hints.append(tts_pb2.Hints(pitch_shift=opts.pitch_shift))
    return hints


class _ChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: TTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    def _build_request(self, text: str) -> tts_pb2.UtteranceSynthesisRequest:
        return tts_pb2.UtteranceSynthesisRequest(
            model=self._opts.model,
            text=text,
            hints=_build_hints(self._opts),
            output_audio_spec=_build_audio_format(self._opts.sample_rate),
            unsafe_mode=self._opts.unsafe_mode,
        )

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        text = self._input_text.strip()
        if not text:
            return
        if len(text) > TTS_MAX_CHARS_PER_REQUEST and not self._opts.unsafe_mode:
            logger.warning(
                "Text exceeds Yandex TTS 250-char limit (%d chars); "
                "consider enabling unsafe_mode or pre-splitting.",
                len(text),
            )

        request_id = utils.shortuuid()
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._opts.sample_rate,
            num_channels=self._opts.num_channels,
            mime_type="audio/pcm",
        )

        async def _do_synthesize() -> None:
            channel = await self._tts._ensure_channel()
            stub = tts_service_pb2_grpc.SynthesizerStub(channel)
            metadata = await self._tts._credentials.grpc_metadata()
            call = stub.UtteranceSynthesis(
                self._build_request(text),
                metadata=metadata,
                timeout=self._conn_options.timeout,
            )
            async for response in call:
                if response.audio_chunk.data:
                    output_emitter.push(response.audio_chunk.data)
            output_emitter.flush()

        started = time.monotonic()
        try:
            await with_retry(
                "TTS.UtteranceSynthesis",
                _do_synthesize,
                conn_options=self._conn_options,
            )
        except asyncio.TimeoutError as e:
            raise APITimeoutError("Yandex TTS request timed out") from e
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._tts._synthesized_chars += len(text)
            logger.debug(
                "Yandex TTS utterance synthesis completed in %.1f ms (chars=%d, request_id=%s)",
                elapsed_ms,
                len(text),
                request_id,
            )


class SynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    def _build_options(self) -> tts_pb2.SynthesisOptions:
        return tts_pb2.SynthesisOptions(
            model=self._opts.model,
            voice=self._opts.voice,
            role=self._opts.role or "",
            speed=self._opts.speed,
            volume=self._opts.volume,
            pitch_shift=self._opts.pitch_shift,
            output_audio_spec=_build_audio_format(self._opts.sample_rate),
        )

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        segments_ch = utils.aio.Chan[tokenize.SentenceStream]()

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._opts.sample_rate,
            num_channels=self._opts.num_channels,
            mime_type="audio/pcm",
            stream=True,
        )

        async def tokenize_input() -> None:
            sent_stream: tokenize.SentenceStream | None = None
            try:
                async for item in self._input_ch:
                    if isinstance(item, str):
                        if sent_stream is None:
                            sent_stream = self._opts.tokenizer.stream()
                            segments_ch.send_nowait(sent_stream)
                        sent_stream.push_text(item)
                    elif isinstance(item, self._FlushSentinel):
                        if sent_stream is not None:
                            sent_stream.end_input()
                        sent_stream = None
            finally:
                if sent_stream is not None:
                    sent_stream.end_input()
                segments_ch.close()

        async def run_segments() -> None:
            async for sent_stream in segments_ch:
                await self._synthesize_segment(sent_stream, output_emitter)

        tasks = [
            asyncio.create_task(tokenize_input(), name="yandex-tts-tokenize"),
            asyncio.create_task(run_segments(), name="yandex-tts-segments"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            await utils.aio.cancel_and_wait(*tasks)

    async def _synthesize_segment(
        self,
        sent_stream: tokenize.SentenceStream,
        output_emitter: tts.AudioEmitter,
    ) -> None:
        channel = await self._tts._ensure_channel()
        stub = tts_service_pb2_grpc.SynthesizerStub(channel)
        metadata = await self._tts._credentials.grpc_metadata()
        options = self._build_options()
        segment_chars = 0

        @utils.log_exceptions(logger=logger)
        async def request_generator() -> AsyncGenerator[tts_pb2.StreamSynthesisRequest, None]:
            nonlocal segment_chars
            yield tts_pb2.StreamSynthesisRequest(options=options)
            async for token in sent_stream:
                self._mark_started()
                segment_chars += len(token.token)
                yield tts_pb2.StreamSynthesisRequest(
                    synthesis_input=tts_pb2.SynthesisInput(text=token.token)
                )
            yield tts_pb2.StreamSynthesisRequest(
                force_synthesis=tts_pb2.ForceSynthesisEvent()
            )

        gen = request_generator()
        started = time.monotonic()
        segment_id = utils.shortuuid()
        try:
            call = stub.StreamSynthesis(
                gen,
                metadata=metadata,
                timeout=self._conn_options.timeout,
            )
            output_emitter.start_segment(segment_id=segment_id)
            async for resp in call:
                if resp.audio_chunk.data:
                    output_emitter.push(resp.audio_chunk.data)
            output_emitter.end_segment()
        except grpc.aio.AioRpcError as e:
            raise_from_grpc(e, action="TTS.StreamSynthesis")
        except asyncio.TimeoutError as e:
            raise APITimeoutError("Yandex TTS stream timed out") from e
        finally:
            await gen.aclose()
            self._tts._synthesized_chars += segment_chars
            logger.debug(
                "Yandex TTS stream segment %s completed in %.1f ms (chars=%d)",
                segment_id,
                (time.monotonic() - started) * 1000,
                segment_chars,
            )
