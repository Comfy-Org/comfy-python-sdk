"""The bearer token must never leak to a host a signed download URL redirects to.

``GET /assets/{id}/content`` may serve bytes directly or ``302``-redirect to a
signed URL on a different host (a CDN or blob-storage origin, for example — see
``comfy_sdk/outputs.py``). ``httpx`` (with ``follow_redirects=True``, as the
transport is configured) strips the ``Authorization`` header itself whenever a
redirect crosses origins, but nothing in this SDK's own test suite exercised
that path for the asset-content download — every existing download test only
ever hit the same origin. This locks in that a cross-origin content redirect
really does drop the credential before the SDK's own protection (the
same-origin check in ``comfy_low.transport._Prepared.headers``, covered by
``test_transport_security.py`` for job follow-up links) even comes into play.
"""

from __future__ import annotations

from comfy_sdk import Comfy


def _wf(client: Comfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_cross_origin_content_redirect_does_not_leak_bearer_token(server, second_server) -> None:
    server.state.require_auth = True
    # `server` 302s the content download to a distinct origin (`second_server`).
    redirect_url = f"{second_server.base_url}/api/v2/assets/asset_out_01/content"
    server.state.redirect_content_to = redirect_url
    second_server.state.content_bytes = b"served-by-a-completely-different-host"

    with Comfy(api_key="ck_super_secret") as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        data = out.to_bytes()

    # The redirect was actually followed: bytes came from the second host.
    assert data == second_server.state.content_bytes
    # The original request (and the job lifecycle calls) reached `server` with
    # the bearer token, same as always...
    assert server.state.last_auth_header == "Bearer ck_super_secret"
    # ...but it must NOT have been forwarded across the redirect.
    assert second_server.state.last_auth_header == ""


def test_cross_origin_content_redirect_to_file_does_not_leak_bearer_token(
    server, second_server, tmp_path
) -> None:
    # Same guarantee via the streaming-to-disk path (`to_file`), not just
    # the buffering `to_bytes` path — they use separate iteration code.
    server.state.require_auth = True
    redirect_url = f"{second_server.base_url}/api/v2/assets/asset_out_01/content"
    server.state.redirect_content_to = redirect_url
    second_server.state.content_bytes = b"also-served-by-the-other-host"

    with Comfy(api_key="ck_super_secret") as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        dest = out.to_file(tmp_path / "out.bin")

    assert dest.read_bytes() == second_server.state.content_bytes
    assert second_server.state.last_auth_header == ""
