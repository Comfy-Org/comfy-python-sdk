"""The SDK identifies itself via ``User-Agent`` on every request (request
metadata, not telemetry — no phone-home), so adoption is measurable
server-side. An optional ``client_info`` lets an integration attribute its
own traffic via an ``app/{name}`` token, without clobbering the base identity.
"""

from __future__ import annotations

import pytest

from comfy_sdk import Comfy


def _wf(client: Comfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_user_agent_identifies_the_sdk(server) -> None:
    with Comfy() as client:
        job = client.submit(_wf(client))
        job.refresh()
    ua = server.state.last_user_agent or ""
    assert ua.startswith("comfy-sdk-python/")
    assert "(" in ua and ")" in ua  # runtime segment
    assert "app/" not in ua  # no client_info set


def test_client_info_appends_app_token(server) -> None:
    with Comfy(client_info="glary-bot") as client:
        job = client.submit(_wf(client))
        job.refresh()
    ua = server.state.last_user_agent or ""
    assert ua.startswith("comfy-sdk-python/")
    assert ua.endswith(" app/glary-bot")


def test_client_info_rejects_crlf() -> None:
    # A CR/LF in the caller token must never reach the header (no injection).
    for bad in ("evil\r\nX-Injected: 1", "line\nbreak", "carriage\rreturn"):
        with pytest.raises(ValueError):
            Comfy(client_info=bad)
