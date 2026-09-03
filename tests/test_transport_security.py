"""The bearer token must never leak to a host other than the configured
``base_url``.

``job.urls.self`` / ``job.urls.events`` / ``job.urls.cancel`` / ``job.urls.logs``
are server-returned absolute URLs that ``Job.refresh()`` / ``events()`` /
``cancel()`` / ``get_logs()`` hand straight to the transport (see
``comfy_sdk/jobs.py``). The guard is generic — it keys on the resolved origin in
``_Prepared.headers``, not on which link produced the URL — so a new follow-up
link is covered the moment it is added. Before this fix, ``_Prepared``
attached ``Authorization: Bearer <key>`` to *any* absolute URL with no origin
check — a malicious or misconfigured server could point a job's follow-up link
at an attacker-controlled host and have the client hand it the credential.

The rule is "one of this client's **configured** targets", not "base_url", and
there are two of them: the ``/api/v2`` deployment and Comfy Router
(``router_base_url``), which is a different host by default and would answer
every model run ``401`` without the key. Both come from the client's own
construction — a constant or an environment variable the operator set — never
from a server response, which is what keeps the second target from widening
what a malicious server can aim the credential at. A third origin still gets
nothing, and that is asserted here in both directions.
"""

from __future__ import annotations

import pytest

from comfy_low.transport import ComfyLow


def test_absolute_url_same_origin_still_gets_bearer_token(server) -> None:
    server.state.require_auth = True
    with ComfyLow(server.base_url, api_key="ck_test") as low:
        # A same-origin absolute URL (exactly the shape job.urls.self takes).
        low.get_job(f"{server.base_url}/api/v2/jobs/whatever")
    assert server.state.last_auth_header == "Bearer ck_test"


def test_absolute_url_cross_origin_does_not_get_bearer_token(server, second_server) -> None:
    # The client is configured against `server` with a real key. A job's
    # `urls.self` pointing at `second_server` — a different origin — must NOT
    # receive that key, even though it's a plain absolute URL our own transport
    # is asked to fetch (not an httpx redirect, where httpx already strips auth
    # on its own).
    with ComfyLow(server.base_url, api_key="ck_super_secret") as low:
        low.get_job(f"{second_server.base_url}/api/v2/jobs/whatever")
    assert second_server.state.last_auth_header == ""
    # And the same client still authenticates correctly against its own origin.
    with ComfyLow(server.base_url, api_key="ck_super_secret") as low:
        low.get_job(f"{server.base_url}/api/v2/jobs/whatever")
    assert server.state.last_auth_header == "Bearer ck_super_secret"


def test_relative_path_resolved_against_base_url_still_gets_token(server) -> None:
    # A plain job id (not a URL) resolves under base_url — unaffected by the fix.
    server.state.require_auth = True
    with ComfyLow(server.base_url, api_key="ck_test") as low:
        low.get_job("whatever")
    assert server.state.last_auth_header == "Bearer ck_test"


# --- the second configured target: Comfy Router --------------------------

MODEL = "acme/flux-dev"


def test_the_router_origin_gets_the_token_even_when_it_is_not_base_url(
    server, second_server
) -> None:
    # The production shape: two different hosts, one credential. Without this
    # the headline model API would be unauthenticated against a correctly
    # configured client.
    second_server.state.require_auth = True
    with ComfyLow(
        server.base_url, api_key="ck_test", router_base_url=second_server.base_url
    ) as low:
        low.post_model_run(MODEL, {"prompt": "a cat"})
    assert second_server.state.last_auth_header == "Bearer ck_test"
    assert second_server.state.last_model_run_path == "/v2/models/acme/flux-dev"


def test_a_third_origin_still_gets_no_token_once_the_router_is_trusted(
    server, second_server
) -> None:
    # The regression this pairs with: broadening the origin check from one
    # configured target to two must not broaden it to "any absolute URL".
    # `second_server` is neither `base_url` nor `router_base_url` here.
    with ComfyLow(
        server.base_url, api_key="ck_super_secret", router_base_url="https://router.example"
    ) as low:
        low.get_job(f"{second_server.base_url}/api/v2/jobs/whatever")
    assert second_server.state.last_auth_header == ""


def test_the_run_url_does_not_pick_up_the_v2_prefix(server) -> None:
    # `_Prepared.url()` prepends `/api/v2` to a relative path; the model-run URL
    # is built absolute against `router_base_url` precisely so it does not.
    with ComfyLow(server.base_url, api_key="ck_test", router_base_url=server.base_url) as low:
        low.post_model_run(MODEL, {"prompt": "a cat"})
    assert server.state.last_model_run_path == "/v2/models/acme/flux-dev"


@pytest.mark.parametrize("bad", ["ftp://h", "router.example", "//router.example", ""])
def test_a_non_http_router_base_url_is_refused_at_construction(bad: str) -> None:
    # Otherwise `url()` — which passes a string through only when it starts with
    # `http` — would fall through to the `base_url + /api/v2 + ...` branch and
    # send an authenticated run at a mangled URL on the wrong surface.
    with pytest.raises(ValueError, match="router_base_url"):
        ComfyLow("https://cloud.comfy.org", api_key="ck_test", router_base_url=bad)
