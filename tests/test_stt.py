from __future__ import annotations

import pytest

from livekit.plugins.yandex import STT


@pytest.fixture
def stt(monkeypatch: pytest.MonkeyPatch) -> STT:
    monkeypatch.setenv("YANDEX_API_KEY", "test_key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test_folder")
    return STT(model="general", language="ru-RU")


def test_defaults(stt: STT) -> None:
    assert stt._opts.language == "ru-RU"
    assert stt._opts.model == "general"
    assert stt._opts.sample_rate == 8000
    assert stt.capabilities.streaming is True
    assert stt.capabilities.interim_results is True


def test_update_options(stt: STT) -> None:
    stt.update_options(language="en-US", model="general:rc")
    assert stt._opts.language == "en-US"
    assert stt._opts.model == "general:rc"


def test_warns_without_folder_id(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("YANDEX_API_KEY", "test_key")
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    with caplog.at_level("WARNING", logger="livekit.plugins.yandex"):
        STT()
    assert any("YANDEX_FOLDER_ID" in rec.message for rec in caplog.records)
