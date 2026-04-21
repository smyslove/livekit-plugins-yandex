from __future__ import annotations

import pytest

from livekit.plugins.yandex import TTS
from livekit.plugins.yandex._proto.yandex.cloud.ai.tts.v3 import tts_pb2
from livekit.plugins.yandex.tts import _ChunkedStream


@pytest.fixture
def tts(monkeypatch: pytest.MonkeyPatch) -> TTS:
    monkeypatch.setenv("YANDEX_API_KEY", "test_key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test_folder")
    return TTS(voice="alena", sample_rate=8000)


def test_defaults(tts: TTS) -> None:
    assert tts.sample_rate == 8000
    assert tts.num_channels == 1
    assert tts.provider == "yandex"
    assert tts.capabilities.streaming is True


def test_rejects_invalid_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_API_KEY", "k")
    with pytest.raises(ValueError, match="speed"):
        TTS(speed=10.0)


async def test_build_request_contains_voice_and_audio_format(tts: TTS) -> None:
    from livekit.agents import DEFAULT_API_CONNECT_OPTIONS

    stream = _ChunkedStream(tts=tts, input_text="Hello", conn_options=DEFAULT_API_CONNECT_OPTIONS)
    try:
        req = stream._build_request("Hello")
        assert req.text == "Hello"
        assert req.output_audio_spec.raw_audio.audio_encoding == tts_pb2.RawAudio.LINEAR16_PCM
        assert req.output_audio_spec.raw_audio.sample_rate_hertz == 8000
        voice_hints = [h for h in req.hints if h.WhichOneof("Hint") == "voice"]
        assert voice_hints and voice_hints[0].voice == "alena"
    finally:
        await stream.aclose()


def test_update_options(tts: TTS) -> None:
    tts.update_options(voice="filipp", speed=1.5)
    assert tts._opts.voice == "filipp"
    assert tts._opts.speed == 1.5
