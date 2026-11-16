"""``client.check()``: confirm the key and the address without running anything.

The probe is a lookup of a job id no deployment issues. A deployment that took
the key answers ``404 job_not_found``; everything else is a reason the first
real run would fail, and each one surfaces as the error that names its fix.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from comfy_sdk import AsyncComfy, CheckResult, Comfy, ComfyError
from comfy_sdk.exceptions import Unauthorized

_ZERO = "00000000-0000-0000-0000-000000000000"


def _check(client_cls: type, **kwargs: Any) -> CheckResult:
    if client_cls is Comfy:
        with Comfy(**kwargs) as client:
            return client.check()

    async def go() -> CheckResult:
        async with AsyncComfy(**kwargs) as client:
            return await client.check()

    return asyncio.run(go())


@pytest.fixture(params=[Comfy, AsyncComfy], ids=["sync", "async"])
def client_cls(request: pytest.FixtureRequest) -> type:
    return request.param


def test_passes_on_job_not_found_and_runs_nothing(server, client_cls) -> None:
    server.state.job_not_found = True
    result = _check(client_cls, api_key="comfyui-test")
    assert result == CheckResult(base_url=server.base_url, authenticated=True)
    assert server.state.job_get_ids == [_ZERO]
    assert server.state.submit_count == 0


def test_sends_the_key(server, client_cls) -> None:
    server.state.job_not_found = True
    server.state.require_auth = True
    _check(client_cls, api_key="comfyui-test")
    assert server.state.last_auth_header == "Bearer comfyui-test"


def test_keyless_self_hosted_passes_unauthenticated(server, client_cls) -> None:
    server.state.job_not_found = True
    assert _check(client_cls).authenticated is False


def test_rejected_key_raises_unauthorized(server, client_cls) -> None:
    server.state.require_auth = True
    with pytest.raises(Unauthorized):
        _check(client_cls)


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (404, "application/json", b'{"error": {"code": "not_found", "message": "Not Found"}}'),
        (200, "text/html", b"<!doctype html><html><body>app</body></html>"),
    ],
    ids=["bare-404", "web-page"],
)
def test_wrong_address_raises_not_a_comfy_api(
    server, client_cls, status: int, content_type: str, body: bytes
) -> None:
    server.state.job_get_raw = (status, content_type, body)
    with pytest.raises(ComfyError) as info:
        _check(client_cls, api_key="comfyui-test")
    assert info.value.code == "not_a_comfy_api"
    assert "COMFY_BASE_URL" in str(info.value)


def test_a_job_for_the_probe_id_is_an_invalid_response(server, client_cls) -> None:
    with pytest.raises(ComfyError) as info:
        _check(client_cls, api_key="comfyui-test")
    assert info.value.code == "invalid_response"


def test_server_error_keeps_its_code(server, client_cls) -> None:
    server.state.job_get_raw = (
        503,
        "application/json",
        b'{"error": {"code": "service_unavailable", "message": "down"}}',
    )
    with pytest.raises(ComfyError) as info:
        _check(client_cls, api_key="comfyui-test")
    assert info.value.code == "service_unavailable"
    assert info.value.http_status == 503


def test_the_key_never_appears_in_the_result(server, client_cls) -> None:
    server.state.job_not_found = True
    result = _check(client_cls, api_key="comfyui-secret-value")
    assert "secret" not in repr(result)
