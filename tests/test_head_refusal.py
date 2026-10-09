"""A refused ``HEAD /assets/by-hash`` has no body, so its code comes from the status.

The gateway cannot send the error envelope on a ``HEAD``, so the generic
status fallback would read its ``429`` account rate limit as ``queue_full``.
``head_asset_by_hash`` synthesizes ``rate_limited`` for that ``429`` instead,
keeping ``Retry-After``; a ``403`` stays ``forbidden``.
"""

from __future__ import annotations

import pytest

from comfy_low.errors import ApiError, Forbidden, QueueFull
from comfy_low.transport import AsyncComfyLow, ComfyLow
from comfy_sdk import AsyncComfy, Comfy, ComfyError
from comfy_sdk import Forbidden as SdkForbidden
from comfy_sdk import QueueFull as SdkQueueFull

_HASH = "blake3:" + "0" * 64


def _assert_rate_limited(exc: ApiError, retry_after: int | None) -> None:
    assert not isinstance(exc, QueueFull)
    assert exc.code == "rate_limited"
    assert exc.http_status == 429
    assert exc.retry_after == retry_after


@pytest.mark.parametrize(("headers", "retry_after"), [({"Retry-After": "7"}, 7), ({}, None)])
def test_bodiless_429_is_rate_limited(server, headers, retry_after) -> None:
    server.state.head_refusal = (429, headers)
    with ComfyLow(server.base_url) as low, pytest.raises(ApiError) as info:
        low.head_asset_by_hash(_HASH)
    _assert_rate_limited(info.value, retry_after)


@pytest.mark.parametrize(("headers", "retry_after"), [({"Retry-After": "7"}, 7), ({}, None)])
async def test_bodiless_429_is_rate_limited_async(server, headers, retry_after) -> None:
    server.state.head_refusal = (429, headers)
    async with AsyncComfyLow(server.base_url) as low:
        with pytest.raises(ApiError) as info:
            await low.head_asset_by_hash(_HASH)
    _assert_rate_limited(info.value, retry_after)


def test_bodiless_403_is_forbidden(server) -> None:
    server.state.head_refusal = (403, {})
    with ComfyLow(server.base_url) as low, pytest.raises(Forbidden) as info:
        low.head_asset_by_hash(_HASH)
    assert info.value.code == "forbidden"
    assert info.value.http_status == 403


async def test_bodiless_403_is_forbidden_async(server) -> None:
    server.state.head_refusal = (403, {})
    async with AsyncComfyLow(server.base_url) as low:
        with pytest.raises(Forbidden):
            await low.head_asset_by_hash(_HASH)


def test_commit_surfaces_rate_limited_not_queue_full(server, tmp_path) -> None:
    p = tmp_path / "photo.png"
    p.write_bytes(b"rate-limited-bytes")
    server.state.head_refusal = (429, {"Retry-After": "7"})

    with Comfy() as client, pytest.raises(ComfyError) as info:
        client.assets.from_file(p).commit()

    assert not isinstance(info.value, SdkQueueFull)
    assert info.value.code == "rate_limited"
    assert info.value.http_status == 429
    assert server.state.upload_count == 0


async def test_commit_surfaces_rate_limited_not_queue_full_async(server, tmp_path) -> None:
    p = tmp_path / "photo.png"
    p.write_bytes(b"rate-limited-bytes")
    server.state.head_refusal = (429, {"Retry-After": "7"})

    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as info:
            await client.assets.from_file(p).commit()

    assert not isinstance(info.value, SdkQueueFull)
    assert info.value.code == "rate_limited"


def test_commit_surfaces_forbidden(server, tmp_path) -> None:
    p = tmp_path / "photo.png"
    p.write_bytes(b"forbidden-bytes")
    server.state.head_refusal = (403, {})

    with Comfy() as client, pytest.raises(SdkForbidden):
        client.assets.from_file(p).commit()
