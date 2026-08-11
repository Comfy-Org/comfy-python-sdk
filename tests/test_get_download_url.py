"""``get_download_url()`` — a directly-fetchable URL without streaming bytes.

Covers both shapes a backend can answer with (see ``comfy_low.transport.
get_asset_content_url``): a redirect to a signed URL (Cloud/serverless, backed
by object storage) and an inline 200/206 body (self-hosted, no redirect at
all) — plus that each output on a multi-output job resolves its own distinct
URL, and that the bearer token is never sent to the redirect target because
the redirect is never followed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import _output_json

from comfy_low.transport import AsyncComfyLow, ComfyLow, parse_expiry
from comfy_sdk import AsyncComfy, Comfy, DownloadUrl

_SIGNED_URL = (
    "https://storage.googleapis.com/bucket/object"
    "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
    "&X-Goog-Credential=example%2F20260710%2Fauto%2Fstorage%2Fgoog4_request"
    "&X-Goog-Date=20260710T180000Z"
    "&X-Goog-Expires=3600"
    "&X-Goog-SignedHeaders=host"
    "&X-Goog-Signature=deadbeef"
)


def _wf(client: Comfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


# -- parse_expiry -----------------------------------------------------------


def test_parse_expiry_reads_goog_date_and_expires() -> None:
    assert parse_expiry(_SIGNED_URL) == datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)


def test_parse_expiry_returns_none_when_params_absent() -> None:
    assert parse_expiry("https://example.invalid/assets/x/content") is None


def test_parse_expiry_returns_none_on_malformed_values() -> None:
    bad_date = "https://example.invalid?X-Goog-Date=not-a-date&X-Goog-Expires=60"
    bad_expires = "https://example.invalid?X-Goog-Date=20260710T180000Z&X-Goog-Expires=x"
    assert parse_expiry(bad_date) is None
    assert parse_expiry(bad_expires) is None


def test_parse_expiry_returns_none_on_overflowing_expires() -> None:
    # A huge expires parses as an int but overflows datetime addition — treat
    # it as "no expiry" rather than raising OverflowError.
    huge = "https://example.invalid?X-Goog-Date=20260710T180000Z&X-Goog-Expires=999999999999999999"
    assert parse_expiry(huge) is None


# -- cloud path: redirect to a signed URL ------------------------------------


def test_cloud_redirect_returns_signed_url_and_computed_expiry(server) -> None:
    server.state.redirect_content_to = _SIGNED_URL
    with ComfyLow(server.base_url) as low:
        url, expires_at = low.get_asset_content_url("asset_out_01")
    assert url == _SIGNED_URL
    assert expires_at == datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)


async def test_async_cloud_redirect_returns_signed_url_and_computed_expiry(server) -> None:
    server.state.redirect_content_to = _SIGNED_URL
    async with AsyncComfyLow(server.base_url) as low:
        url, expires_at = await low.get_asset_content_url("asset_out_01")
    assert url == _SIGNED_URL
    assert expires_at == datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)


def test_output_get_download_url_on_cloud_redirect(server) -> None:
    server.state.redirect_content_to = _SIGNED_URL
    with Comfy() as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        download = out.get_download_url()
    assert isinstance(download, DownloadUrl)
    assert download.url == _SIGNED_URL
    assert download.expires_at == datetime(2026, 7, 10, 19, 0, 0, tzinfo=timezone.utc)


# -- self-hosted path: inline 200, no redirect -------------------------------


def test_self_hosted_returns_content_url_and_no_expiry(server) -> None:
    # No `redirect_content_to` configured -> the stub serves bytes inline
    # (200), the never-throws / works-everywhere path.
    with ComfyLow(server.base_url) as low:
        url, expires_at = low.get_asset_content_url("asset_out_01")
    assert url == f"{server.base_url}/api/v2/assets/asset_out_01/content"
    assert expires_at is None


async def test_async_self_hosted_returns_content_url_and_no_expiry(server) -> None:
    async with AsyncComfyLow(server.base_url) as low:
        url, expires_at = await low.get_asset_content_url("asset_out_01")
    assert url == f"{server.base_url}/api/v2/assets/asset_out_01/content"
    assert expires_at is None


def test_output_get_download_url_on_self_hosted(server) -> None:
    with Comfy() as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        download = out.get_download_url()  # must not throw
    assert download.url == f"{server.base_url}/api/v2/assets/asset_out_01/content"
    assert download.expires_at is None


# -- multiple outputs: each resolves its own distinct URL --------------------


def test_multi_output_job_each_get_download_url_is_distinct(server) -> None:
    server.state.job_outputs = [
        _output_json("13", "asset_out_01"),
        _output_json("14", "asset_out_02"),
    ]
    with Comfy() as client:
        job = client.run(_wf(client))
        urls = {out.id: out.get_download_url().url for out in job.outputs}

    assert len(job.outputs) == 2
    assert urls["asset_out_01"] == f"{server.base_url}/api/v2/assets/asset_out_01/content"
    assert urls["asset_out_02"] == f"{server.base_url}/api/v2/assets/asset_out_02/content"
    assert urls["asset_out_01"] != urls["asset_out_02"]


async def test_async_multi_output_job_each_get_download_url_is_distinct(server) -> None:
    server.state.job_outputs = [
        _output_json("13", "asset_out_01"),
        _output_json("14", "asset_out_02"),
    ]
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        urls = {}
        for out in job.outputs:
            download = await out.get_download_url()
            urls[out.id] = download.url

    assert len(job.outputs) == 2
    assert urls["asset_out_01"] != urls["asset_out_02"]


# -- the redirect is never followed: no bearer token reaches the other host --


def test_redirect_target_never_contacted_bearer_never_leaked(server, second_server) -> None:
    server.state.require_auth = True
    redirect_url = f"{second_server.base_url}/api/v2/assets/asset_out_01/content"
    server.state.redirect_content_to = redirect_url

    with Comfy(api_key="ck_super_secret") as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        download = out.get_download_url()

    assert download.url == redirect_url
    # The original request reached `server` with the bearer token as always...
    assert server.state.last_auth_header == "Bearer ck_super_secret"
    # ...but the redirect target was never even contacted (it isn't followed),
    # so it never saw a request, let alone the credential.
    assert second_server.state.last_auth_header is None
