from __future__ import annotations

import aiohttp
import grpc
import pytest

from livekit.agents import (
    APIConnectOptions,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from livekit.plugins.yandex._retry import compute_backoff, raise_from_grpc, with_retry


class _FakeGrpcError(grpc.aio.AioRpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "boom") -> None:
        super().__init__(
            code=code,
            initial_metadata=grpc.aio.Metadata(),
            trailing_metadata=grpc.aio.Metadata(),
            details=details,
        )


def test_compute_backoff_bounded_by_cap() -> None:
    for attempt in range(1, 20):
        delay = compute_backoff(attempt, base=1.0, cap=5.0)
        assert 0 <= delay <= 5.0


def test_raise_from_grpc_maps_codes() -> None:
    with pytest.raises(APITimeoutError):
        raise_from_grpc(_FakeGrpcError(grpc.StatusCode.DEADLINE_EXCEEDED), action="X")

    with pytest.raises(APIConnectionError):
        raise_from_grpc(_FakeGrpcError(grpc.StatusCode.UNAVAILABLE), action="X")

    with pytest.raises(APIStatusError) as ei:
        raise_from_grpc(_FakeGrpcError(grpc.StatusCode.RESOURCE_EXHAUSTED), action="X")
    assert ei.value.status_code == 429

    with pytest.raises(APIStatusError) as ei:
        raise_from_grpc(_FakeGrpcError(grpc.StatusCode.INVALID_ARGUMENT), action="X")
    assert ei.value.status_code != 429


async def test_with_retry_succeeds_after_transient_error() -> None:
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "retry me")
        return "ok"

    result = await with_retry(
        "X",
        flaky,
        conn_options=APIConnectOptions(max_retry=5, retry_interval=0.01, timeout=5),
    )
    assert result == "ok"
    assert len(attempts) == 3


async def test_with_retry_exhausts_and_maps() -> None:
    async def always_unavailable() -> None:
        raise _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "down")

    with pytest.raises(APIConnectionError):
        await with_retry(
            "X",
            always_unavailable,
            conn_options=APIConnectOptions(max_retry=1, retry_interval=0.01, timeout=5),
        )


async def test_with_retry_does_not_retry_non_transient() -> None:
    attempts: list[int] = []

    async def bad_request() -> None:
        attempts.append(1)
        raise _FakeGrpcError(grpc.StatusCode.INVALID_ARGUMENT, "bad")

    with pytest.raises(APIStatusError):
        await with_retry(
            "X",
            bad_request,
            conn_options=APIConnectOptions(max_retry=5, retry_interval=0.01, timeout=5),
        )
    assert len(attempts) == 1


async def test_with_retry_maps_http_429() -> None:
    from unittest.mock import Mock

    async def rate_limited() -> None:
        raise aiohttp.ClientResponseError(
            Mock(real_url="http://test", method="POST"),
            history=(),
            status=429,
            message="too many",
            headers=None,
        )

    with pytest.raises(APIStatusError) as ei:
        await with_retry(
            "X",
            rate_limited,
            conn_options=APIConnectOptions(max_retry=1, retry_interval=0.01, timeout=5),
        )
    assert ei.value.status_code == 429
