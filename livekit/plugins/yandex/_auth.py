"""Authentication helpers for Yandex SpeechKit.

Three credential strategies are supported:

    * **API key** — long-lived service-account key (`Api-Key <token>`).
      Preferred for MVP. No refresh needed.

    * **IAM token (static)** — short-lived (~12 h) `Bearer` token supplied by the
      caller. No refresh is performed; caller is responsible for rotation.

    * **OAuth → IAM refresher** — exchanges a Yandex Passport OAuth token for an
      IAM token and refreshes it in the background before expiry. Use this for
      long-running services that outlive a single IAM token lifetime.

All impls expose the same async interface (`grpc_metadata`, `http_headers`,
`aclose`) so the TTS/STT call-path doesn't care which one is wired up.
"""

from __future__ import annotations

import abc
import asyncio
import os
from datetime import datetime, timezone

import aiohttp

from .log import logger

# Yandex IAM token exchange endpoint.
_IAM_TOKEN_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"

# Refresh an IAM token this many seconds before it expires.
_IAM_REFRESH_MARGIN_S = 60 * 60  # 1 hour
# Hard cap on the interval between scheduled refreshes (cloud might issue
# unexpectedly long-lived tokens; we still want a periodic check-in).
_IAM_MAX_REFRESH_INTERVAL_S = 10 * 60 * 60  # 10 hours
# After a transient error when refreshing, retry with this delay.
_IAM_RETRY_ON_ERROR_S = 30


class YandexAuthError(ValueError):
    """Raised when Yandex credentials are missing or malformed."""


class YandexCredentials(abc.ABC):
    """Abstract Yandex credentials."""

    def __init__(self, folder_id: str | None) -> None:
        self._folder_id = folder_id

    @property
    def folder_id(self) -> str | None:
        return self._folder_id

    @abc.abstractmethod
    async def _authorization(self) -> str:
        """Return the `authorization` header value, e.g. `Api-Key <token>`."""

    async def grpc_metadata(self) -> list[tuple[str, str]]:
        md: list[tuple[str, str]] = [("authorization", await self._authorization())]
        if self._folder_id:
            md.append(("x-folder-id", self._folder_id))
        return md

    async def http_headers(self) -> dict[str, str]:
        return {"Authorization": await self._authorization()}

    async def aclose(self) -> None:
        """Release any resources (e.g. background refresh tasks)."""
        return None


class _ApiKeyCredentials(YandexCredentials):
    def __init__(self, api_key: str, folder_id: str | None) -> None:
        super().__init__(folder_id)
        self._api_key = api_key

    @property
    def api_key(self) -> str:
        return self._api_key

    async def _authorization(self) -> str:
        return f"Api-Key {self._api_key}"


class _StaticIamTokenCredentials(YandexCredentials):
    def __init__(self, iam_token: str, folder_id: str | None) -> None:
        super().__init__(folder_id)
        self._iam_token = iam_token

    async def _authorization(self) -> str:
        return f"Bearer {self._iam_token}"


class _OAuthRefreshingCredentials(YandexCredentials):
    """Exchange a Yandex OAuth token for an IAM token; refresh in the background."""

    def __init__(
        self,
        *,
        oauth_token: str,
        folder_id: str | None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(folder_id)
        self._oauth_token = oauth_token
        self._session = http_session
        self._owns_session = http_session is None
        self._iam_token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    async def _authorization(self) -> str:
        async with self._lock:
            if self._iam_token is None or self._is_near_expiry():
                await self._refresh_locked()
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(
                    self._refresh_loop(), name="yandex-iam-refresh"
                )
        return f"Bearer {self._iam_token}"

    def _is_near_expiry(self) -> bool:
        if self._expires_at is None:
            return True
        return (self._expires_at - datetime.now(timezone.utc)).total_seconds() < _IAM_REFRESH_MARGIN_S

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _refresh_locked(self) -> None:
        token, expires_at = await self._exchange()
        self._iam_token = token
        self._expires_at = expires_at
        logger.debug(
            "Yandex IAM token refreshed, expires at %s",
            expires_at.isoformat() if expires_at else "unknown",
        )

    async def _exchange(self) -> tuple[str, datetime | None]:
        session = await self._ensure_session()
        async with session.post(
            _IAM_TOKEN_URL,
            json={"yandexPassportOauthToken": self._oauth_token},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as res:
            if res.status != 200:
                body = await res.text()
                raise YandexAuthError(
                    f"Yandex IAM exchange failed ({res.status}): {body}"
                )
            payload = await res.json()
        token = payload.get("iamToken")
        expires_raw = payload.get("expiresAt")
        if not token:
            raise YandexAuthError("Yandex IAM response missing iamToken")
        expires_at: datetime | None = None
        if expires_raw:
            # Yandex returns RFC3339 with 'Z' suffix; Python <3.11 needs manual parsing.
            try:
                expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            except ValueError:
                expires_at = None
        return token, expires_at

    async def _refresh_loop(self) -> None:
        while True:
            delay = self._compute_next_delay()
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            try:
                async with self._lock:
                    await self._refresh_locked()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    "Yandex IAM token refresh failed, retrying in %ss: %s",
                    _IAM_RETRY_ON_ERROR_S,
                    e,
                )

    def _compute_next_delay(self) -> float:
        if self._expires_at is None:
            return _IAM_RETRY_ON_ERROR_S
        remaining = (self._expires_at - datetime.now(timezone.utc)).total_seconds()
        return float(max(1.0, min(_IAM_MAX_REFRESH_INTERVAL_S, remaining - _IAM_REFRESH_MARGIN_S)))

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except BaseException:
                pass
            self._refresh_task = None
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


def resolve_credentials(
    *,
    api_key: str | None = None,
    iam_token: str | None = None,
    oauth_token: str | None = None,
    folder_id: str | None = None,
    http_session: aiohttp.ClientSession | None = None,
) -> YandexCredentials:
    """Build credentials using the first available strategy.

    Resolution order (after falling back to env):

        iam_token > oauth_token > api_key

    Env variables: `YANDEX_API_KEY`, `YANDEX_IAM_TOKEN`, `YANDEX_OAUTH_TOKEN`,
    `YANDEX_FOLDER_ID`.
    """
    api_key = api_key or os.environ.get("YANDEX_API_KEY")
    iam_token = iam_token or os.environ.get("YANDEX_IAM_TOKEN")
    oauth_token = oauth_token or os.environ.get("YANDEX_OAUTH_TOKEN")
    folder_id = folder_id or os.environ.get("YANDEX_FOLDER_ID")

    if iam_token:
        return _StaticIamTokenCredentials(iam_token=iam_token, folder_id=folder_id)
    if oauth_token:
        return _OAuthRefreshingCredentials(
            oauth_token=oauth_token,
            folder_id=folder_id,
            http_session=http_session,
        )
    if api_key:
        return _ApiKeyCredentials(api_key=api_key, folder_id=folder_id)

    raise YandexAuthError(
        "Yandex credentials are required. Set YANDEX_API_KEY (preferred), "
        "YANDEX_IAM_TOKEN, or YANDEX_OAUTH_TOKEN — or pass api_key=/iam_token=/oauth_token= "
        "explicitly."
    )
