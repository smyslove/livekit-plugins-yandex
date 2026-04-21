from __future__ import annotations

import pytest

from livekit.plugins.yandex import STT, TTS
from livekit.plugins.yandex._proto.yandex.cloud.ai.stt.v3 import stt_pb2
from livekit.plugins.yandex._proto.yandex.cloud.ai.tts.v3 import tts_pb2


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_API_KEY", "test_key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test_folder")


async def test_stt_session_options_single_language() -> None:
    stt = STT(language="ru-RU")
    stream = stt.stream()
    try:
        req = stream._build_session_options_request()
        opts = req.session_options
        assert opts.recognition_model.model == "general"
        raw = opts.recognition_model.audio_format.raw_audio
        assert raw.audio_encoding == stt_pb2.RawAudio.LINEAR16_PCM
        assert raw.sample_rate_hertz == 8000
        restr = opts.recognition_model.language_restriction
        assert restr.restriction_type == stt_pb2.LanguageRestrictionOptions.WHITELIST
        assert list(restr.language_code) == ["ru-RU"]
        assert (
            opts.recognition_model.audio_processing_type
            == stt_pb2.RecognitionModelOptions.REAL_TIME
        )
        assert opts.eou_classifier.WhichOneof("Classifier") == "external_classifier"
    finally:
        await stream.aclose()
        await stt.aclose()


async def test_stt_session_options_auto_with_hints() -> None:
    stt = STT(language="auto", language_hints=["ru-RU", "kk-KZ", "en-US"])
    stream = stt.stream()
    try:
        req = stream._build_session_options_request()
        restr = req.session_options.recognition_model.language_restriction
        assert restr.restriction_type == stt_pb2.LanguageRestrictionOptions.WHITELIST
        assert list(restr.language_code) == ["ru-RU", "kk-KZ", "en-US"]
    finally:
        await stream.aclose()
        await stt.aclose()


async def test_stt_session_options_auto_no_restriction() -> None:
    stt = STT(language="auto")
    stream = stt.stream()
    try:
        req = stream._build_session_options_request()
        # No hints and auto language -> no restriction.
        assert not req.session_options.recognition_model.HasField("language_restriction")
    finally:
        await stream.aclose()
        await stt.aclose()


async def test_stt_default_eou_when_external_disabled() -> None:
    stt = STT(external_eou=False)
    stream = stt.stream()
    try:
        req = stream._build_session_options_request()
        assert (
            req.session_options.eou_classifier.WhichOneof("Classifier")
            == "default_classifier"
        )
    finally:
        await stream.aclose()
        await stt.aclose()


async def test_tts_stream_options() -> None:
    tts = TTS(voice="filipp", speed=1.3, role="neutral")
    stream = tts.stream()
    try:
        opts = stream._build_options()
        assert opts.voice == "filipp"
        assert opts.speed == 1.3
        assert opts.role == "neutral"
        assert opts.output_audio_spec.raw_audio.audio_encoding == tts_pb2.RawAudio.LINEAR16_PCM
        assert opts.output_audio_spec.raw_audio.sample_rate_hertz == 8000
    finally:
        await stream.aclose()
        await tts.aclose()


async def test_stt_rejects_bad_language_restriction() -> None:
    with pytest.raises(ValueError):
        STT(language_restriction="bogus")


async def test_stt_update_options_triggers_reconnect() -> None:
    stt = STT(language="ru-RU")
    stream = stt.stream()
    try:
        assert not stream._reconnect_event.is_set()
        stt.update_options(language="en-US")
        assert stream._reconnect_event.is_set()
    finally:
        await stream.aclose()
        await stt.aclose()
