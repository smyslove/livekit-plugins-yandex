from __future__ import annotations

import pytest

from livekit.plugins.yandex._auth import (
    YandexAuthError,
    resolve_credentials,
)


async def test_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_API_KEY", "AQVN_test")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gtest")
    monkeypatch.delenv("YANDEX_IAM_TOKEN", raising=False)
    monkeypatch.delenv("YANDEX_OAUTH_TOKEN", raising=False)
    creds = resolve_credentials()
    try:
        assert creds.folder_id == "b1gtest"
        md = await creds.grpc_metadata()
        assert ("authorization", "Api-Key AQVN_test") in md
        assert ("x-folder-id", "b1gtest") in md
        headers = await creds.http_headers()
        assert headers["Authorization"] == "Api-Key AQVN_test"
    finally:
        await creds.aclose()


async def test_static_iam_wins_over_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_API_KEY", "AQVN_test")
    monkeypatch.setenv("YANDEX_IAM_TOKEN", "t1.iam.test")
    monkeypatch.delenv("YANDEX_OAUTH_TOKEN", raising=False)
    creds = resolve_credentials()
    try:
        md = await creds.grpc_metadata()
        assert ("authorization", "Bearer t1.iam.test") in md
    finally:
        await creds.aclose()


async def test_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_IAM_TOKEN", raising=False)
    monkeypatch.delenv("YANDEX_OAUTH_TOKEN", raising=False)
    with pytest.raises(YandexAuthError):
        resolve_credentials()


async def test_explicit_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YANDEX_API_KEY", "env_key")
    creds = resolve_credentials(api_key="explicit_key", folder_id="f1")
    try:
        md = await creds.grpc_metadata()
        assert ("authorization", "Api-Key explicit_key") in md
        assert ("x-folder-id", "f1") in md
    finally:
        await creds.aclose()


async def test_oauth_refresher_exchanges_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OAuth refresher should POST the oauth token to Yandex IAM and cache the response."""
    from livekit.plugins.yandex._auth import _OAuthRefreshingCredentials

    calls: list[dict] = []

    async def fake_exchange(self):  # type: ignore[no-untyped-def]
        from datetime import datetime, timedelta, timezone

        calls.append({"oauth": self._oauth_token})
        return (f"t1.iam.{len(calls)}", datetime.now(timezone.utc) + timedelta(hours=12))

    monkeypatch.setattr(_OAuthRefreshingCredentials, "_exchange", fake_exchange)

    creds = _OAuthRefreshingCredentials(oauth_token="AQAD_test", folder_id="f1")
    try:
        md1 = await creds.grpc_metadata()
        assert ("authorization", "Bearer t1.iam.1") in md1
        # Second call should use the cached token; _exchange must not be re-invoked.
        md2 = await creds.grpc_metadata()
        assert ("authorization", "Bearer t1.iam.1") in md2
        assert len(calls) == 1
    finally:
        await creds.aclose()
