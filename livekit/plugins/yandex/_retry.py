"""Retry / error-mapping helpers for Yandex SpeechKit calls."""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

import aiohttp
import grpc
import grpc.aio

from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
)

from .log import logger

T = TypeVar("T")

# gRPC status codes that are safe to retry automatically.
_RETRY_GRPC_CODES: frozenset[grpc.StatusCode] = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.INTERNAL,
    }
)

# HTTP status codes that are safe to retry (429 + 5xx).
_RETRY_HTTP_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


def compute_backoff(attempt: int, base: float, cap: float = 30.0) -> float:
    """Full-jitter exponential backoff (AWS-style)."""
    exp = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0, exp)


def raise_from_grpc(e: grpc.aio.AioRpcError, *, action: str) -> None:
    """Map a gRPC error to a LiveKit API error. Always raises."""
    code = e.code()
    details = e.details() or str(e)
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise APITimeoutError(f"Yandex {action} timed out") from e
    if code == grpc.StatusCode.UNAVAILABLE:
        raise APIConnectionError(f"Yandex {action} unavailable: {details}") from e
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
        raise APIStatusError(
            message=f"Yandex {action} rate-limited: {details}",
            status_code=429,
            body=details,
        ) from e
    status = code.value[0] if code else -1
    raise APIStatusError(
        message=f"Yandex {action} error ({code.name if code else 'UNKNOWN'}): {details}",
        status_code=status,
        body=details,
    ) from e


async def with_retry(
    action: str,
    fn: Callable[[], Awaitable[T]],
    *,
    conn_options: APIConnectOptions,
) -> T:
    """Execute `fn` with retries on transient gRPC/HTTP failures.

    Retries are triggered by `UNAVAILABLE`, `RESOURCE_EXHAUSTED`,
    `DEADLINE_EXCEEDED`, transient aiohttp errors, and HTTP 429/5xx.
    """
    max_retry = max(0, conn_options.max_retry)
    base_interval = max(0.1, conn_options.retry_interval)
    last_exc: BaseException | None = None

    for attempt in range(max_retry + 1):
        try:
            return await fn()
        except grpc.aio.AioRpcError as e:
            last_exc = e
            if e.code() not in _RETRY_GRPC_CODES or attempt == max_retry:
                raise_from_grpc(e, action=action)
        except aiohttp.ClientResponseError as e:
            last_exc = e
            if e.status not in _RETRY_HTTP_STATUSES or attempt == max_retry:
                raise APIStatusError(
                    message=f"Yandex {action} HTTP {e.status}: {e.message}",
                    status_code=e.status,
                    body=str(e.message),
                ) from e
        except (aiohttp.ClientConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt == max_retry:
                if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                    raise APITimeoutError(f"Yandex {action} timed out") from e
                raise APIConnectionError(f"Yandex {action} connection error: {e}") from e

        delay = compute_backoff(attempt + 1, base_interval)
        logger.debug(
            "Yandex %s transient failure (attempt %d/%d): %s; retrying in %.2fs",
            action,
            attempt + 1,
            max_retry + 1,
            last_exc,
            delay,
        )
        await asyncio.sleep(delay)

    # Unreachable: the final iteration always raises.
    assert last_exc is not None
    raise APIConnectionError(f"Yandex {action} failed") from last_exc
