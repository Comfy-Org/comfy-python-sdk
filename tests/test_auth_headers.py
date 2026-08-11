"""Per-surface authentication: self-hosted needs no key; Cloud/serverless need one.

Locks in the core claim documented on ``Comfy``/``AsyncComfy`` (and in the
README): a client built with no ``api_key`` must send **no** ``Authorization``
header at all — not an empty one, none — because a self-hosted proxy has no
auth to satisfy and must never see stray credentials on the wire. A client
built with a key attaches it as a bearer token on every request to its own
origin. Existing tests (``test_jobs.py``) only check the *outcome* (401 vs
200) when a key is required; this checks the header actually sent, which is
the part the docs promise.
"""

from __future__ import annotations

from comfy_sdk import Comfy


def _wf(client: Comfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_no_api_key_sends_no_authorization_header_at_all(server) -> None:
    with Comfy() as client:
        job = client.submit(_wf(client))
        job.refresh()
    assert server.state.last_auth_header == ""


def test_api_key_sends_bearer_token_on_every_request(server) -> None:
    server.state.require_auth = True
    with Comfy(api_key="ck_live_test") as client:
        job = client.submit(_wf(client))
        job.refresh()
    assert server.state.last_auth_header == "Bearer ck_live_test"
