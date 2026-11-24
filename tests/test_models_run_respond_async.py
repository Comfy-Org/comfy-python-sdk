"""``models.run(..., respond_async=True)`` — the opt-in queue handoff.

Router answers a run ``202`` with a queue handle, rather than holding the
connection for the finished ``200``, only when the caller sends ``Prefer:
respond-async`` *and* the deployment hands that model off. The SDK's half of
that contract, pinned here against the stub in ``conftest.py``:

* the **default** path never sends ``Prefer``, so an existing caller keeps the
  ``200`` hold-and-return whatever the deployment is configured to do;
* the **opt-in** sends it, alongside the ``Idempotency-Key`` as always;
* a ``202`` **with** ``Preference-Applied: respond-async`` is polled through the
  queue's own status and result reads to the same native result ``run``
  returns for a ``200`` — nothing re-submitted, nothing billed twice;
* a ``200`` despite the opt-in (handoff off, model not handed off) is returned
  as the native body, unchanged;
* a ``202`` **without** ``Preference-Applied`` is never returned as output;
* a resend under the same key, with or without the opt-in, collects the first
  request's generation instead of starting another.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from comfy_low import ApiError
from comfy_low.transport import RunHandoff, _Prepared, model_run_request
from comfy_sdk import AsyncComfy, BinaryResult, Comfy
from comfy_sdk.exceptions import ComfyError
from comfy_sdk.models import RouterRunResult, _preference_applied
from comfy_sdk.router_exceptions import ContentPolicyViolation

MODEL = "acme/flux-dev"
ARGS = {"prompt": "a cat", "steps": 4}


@pytest.fixture
def fast_poll(monkeypatch):
    """Collapse the poll backoff so a multi-poll test is not a multi-second one."""
    import comfy_sdk._core as core

    monkeypatch.setattr(core, "backoff_schedule", lambda *a, **k: iter(lambda: 0.0, None))
    return None


def _client() -> Comfy:
    return Comfy(api_key="comfyui-test-key")


def _async_client() -> AsyncComfy:
    return AsyncComfy(api_key="comfyui-test-key")


# --- the header ------------------------------------------------------------


def test_request_builder_sends_prefer_only_when_asked():
    _, _, default = model_run_request(MODEL, ARGS, "key-1")
    _, _, opted = model_run_request(MODEL, ARGS, "key-1", respond_async=True)
    assert "Prefer" not in default
    assert opted == {"Idempotency-Key": "key-1", "Prefer": "respond-async"}


def test_default_run_sends_no_prefer_even_where_handoff_is_on(server):
    # The handoff is switched on server side, and the default call still gets
    # the 200: without the preference there is nothing to honour.
    server.state.model_run_handoff = True
    with _client() as client:
        result = client.models.run(MODEL, ARGS)
    assert result == server.state.model_run_result
    assert server.state.model_run_prefer_headers == [None]
    assert server.state.queue_status_count == 0


async def test_async_default_run_sends_no_prefer(server):
    server.state.model_run_handoff = True
    async with _async_client() as client:
        result = await client.models.run(MODEL, ARGS)
    assert result == server.state.model_run_result
    assert server.state.model_run_prefer_headers == [None]


def test_opt_in_sends_prefer_and_idempotency_key(server):
    with _client() as client:
        client.models.run(MODEL, ARGS, respond_async=True)
    assert server.state.model_run_prefer_headers == ["respond-async"]
    [key] = server.state.model_run_idempotency_keys
    assert key


# --- 200 despite the opt-in ------------------------------------------------


def test_opt_in_answered_200_returns_the_native_body(server):
    # Handoff off (the default stub), so the preference is ignored server side.
    with _client() as client:
        result = client.models.run(MODEL, ARGS, respond_async=True)
    assert result == server.state.model_run_result
    assert server.state.queue_status_count == 0
    assert server.state.queue_result_count == 0


def test_opt_in_answered_200_binary_is_still_binary(server):
    server.state.model_run_binary_body = b"ID3\x00audio"
    with _client() as client:
        result = client.models.run(MODEL, ARGS, respond_async=True)
    assert isinstance(result, BinaryResult)
    assert result.content == b"ID3\x00audio"


# --- 202 + Preference-Applied: poll the handle ------------------------------


def test_handoff_is_polled_to_the_native_result(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.queue_polls_to_complete = 2
    with _client() as client:
        result = client.models.run(MODEL, ARGS, respond_async=True)
    assert result == server.state.queue_result
    # One run request; the rest are reads of the handle it returned.
    assert server.state.model_run_count == 1
    assert server.state.model_run_generations == 1
    assert server.state.queue_submit_count == 0
    assert server.state.queue_status_count == 3
    assert server.state.queue_result_count == 1
    rid = server.state.queue_request_id
    assert all(f"/v2/models/{MODEL}/requests/{rid}" in p for p in server.state.queue_paths)


async def test_async_handoff_is_polled_to_the_native_result(server, fast_poll):
    server.state.model_run_handoff = True
    async with _async_client() as client:
        result = await client.models.run(MODEL, ARGS, respond_async=True)
    assert result == server.state.queue_result
    assert server.state.model_run_generations == 1
    assert server.state.queue_submit_count == 0
    assert server.state.queue_result_count == 1


def test_handoff_binary_result_is_binary(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.queue_result_binary_body = b"RIFFwav"
    server.state.queue_result_binary_content_type = "audio/wav"
    with _client() as client:
        result = client.models.run(MODEL, ARGS, respond_async=True)
    assert isinstance(result, BinaryResult)
    assert result.content == b"RIFFwav"
    assert result.content_type == "audio/wav"


def test_run_detailed_follows_the_handoff(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.model_run_request_id = "req_run_01"
    # The result route stamps its own id; the run request's is the one kept.
    server.state.queue_result_request_id = "req_result_02"
    with _client() as client:
        detailed = client.models.run_detailed(MODEL, ARGS, respond_async=True)
    assert isinstance(detailed, RouterRunResult)
    assert detailed.output == server.state.queue_result
    assert detailed.replayed is False
    assert detailed.request_id == "req_run_01"
    # Accepted before dispatch: a missing fallback disclosure is "not disclosed".
    assert detailed.handed_off is True
    assert detailed.serving_provider is None


def test_run_detailed_handoff_without_run_request_id_reports_none(server, fast_poll):
    server.state.model_run_handoff = True
    # The 202 names no id; the result fetch's must not stand in for the run's.
    server.state.model_run_request_id = None
    server.state.queue_result_request_id = "req_result_02"
    with _client() as client:
        detailed = client.models.run_detailed(MODEL, ARGS, respond_async=True)
    assert detailed.handed_off is True
    assert detailed.request_id is None


def test_run_detailed_without_handoff_is_not_handed_off(server):
    server.state.model_run_handoff = True
    with _client() as client:
        detailed = client.models.run_detailed(MODEL, ARGS)
    assert detailed.handed_off is False


async def test_async_run_detailed_follows_the_handoff(server, fast_poll):
    server.state.model_run_handoff = True
    async with _async_client() as client:
        detailed = await client.models.run_detailed(MODEL, ARGS, respond_async=True)
    assert detailed.output == server.state.queue_result


def test_handoff_completion_with_error_type_raises_typed(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.queue_error_type = "content_policy_violation"
    with _client() as client, pytest.raises(ContentPolicyViolation) as excinfo:
        client.models.run(MODEL, ARGS, respond_async=True)
    # Carries the run's key, so a resend under it reaches the same request.
    assert excinfo.value.idempotency_key == server.state.model_run_idempotency_keys[0]
    assert server.state.queue_result_count == 0


# --- 202 without Preference-Applied ------------------------------------------


def test_202_without_preference_applied_is_not_a_result(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.model_run_handoff_preference_applied = False
    with _client() as client, pytest.raises(ComfyError) as excinfo:
        client.models.run(MODEL, ARGS, respond_async=True)
    assert excinfo.value.code == "invalid_response"
    assert excinfo.value.idempotency_key == server.state.model_run_idempotency_keys[0]
    assert server.state.queue_status_count == 0
    # The generation it may have started stays reachable through models.handle().
    assert excinfo.value.details["handle"]["request_id"] == server.state.queue_request_id
    assert server.state.queue_request_id in str(excinfo.value)


async def test_async_202_without_preference_applied_is_not_a_result(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.model_run_handoff_preference_applied = False
    async with _async_client() as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.models.run(MODEL, ARGS, respond_async=True)
    assert excinfo.value.code == "invalid_response"


# --- Idempotency-Key across the opt-in ---------------------------------------


def test_retry_inside_one_opted_in_call_keeps_key_and_prefer(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.model_run_fail_times = 1
    server.state.model_run_transient_error = (429, "rate_limited")
    server.state.model_run_retry_after = "0"
    with _client() as client:
        result = client.models.run(MODEL, ARGS, respond_async=True)
    assert result == server.state.queue_result
    keys = server.state.model_run_idempotency_keys
    assert len(keys) == 2 and keys[0] == keys[1]
    assert server.state.model_run_prefer_headers == ["respond-async", "respond-async"]
    assert server.state.model_run_generations == 1


def test_mixed_resend_under_one_key_starts_one_generation(server, fast_poll):
    # The opted-in call was handed off; the caller resends under the same key
    # without the opt-in (a lost response, a different code path). The resend
    # is answered with the same handoff, which is followed rather than raised
    # on, and the model never runs a second time.
    server.state.model_run_handoff = True
    key = "mixed-resend-key-0001"
    with _client() as client:
        first = client.models.run(MODEL, ARGS, idempotency_key=key, respond_async=True)
        second = client.models.run_detailed(MODEL, ARGS, idempotency_key=key)
    assert first == second.output == server.state.queue_result
    assert second.replayed is True
    assert server.state.model_run_idempotency_keys == [key, key]
    assert server.state.model_run_prefer_headers == ["respond-async", None]
    assert server.state.model_run_generations == 1
    assert server.state.queue_submit_count == 0


def test_replayed_handoff_without_preference_applied_is_followed(server, fast_poll):
    # RFC 7240 does not require Preference-Applied on a reply to a request that
    # sent no Prefer, so a replay of the handoff is recognised by being replayed.
    server.state.model_run_handoff = True
    server.state.model_run_handoff_preference_applied = False
    key = "replayed-handoff-key-0001"
    server.state.model_run_handoff_keys.add(key)
    with _client() as client:
        result = client.models.run(MODEL, ARGS, idempotency_key=key)
    assert result == server.state.queue_result
    assert server.state.model_run_generations == 0


def test_followed_handoff_without_opt_in_keeps_the_timeout(server, fast_poll):
    # A default, bounded run answered with a replayed handoff stays bounded.
    server.state.model_run_handoff = True
    server.state.queue_polls_to_complete = 10**9
    key = "bounded-resend-key-0001"
    server.state.model_run_handoff_keys.add(key)
    with _client() as client, pytest.raises(TimeoutError):
        client.models.run(MODEL, ARGS, idempotency_key=key, timeout=0.3)
    assert server.state.queue_cancel_count == 0


async def test_async_followed_handoff_without_opt_in_keeps_the_timeout(server, fast_poll):
    server.state.model_run_handoff = True
    server.state.queue_polls_to_complete = 10**9
    key = "bounded-resend-key-0002"
    server.state.model_run_handoff_keys.add(key)
    async with _async_client() as client:
        with pytest.raises(TimeoutError):
            await client.models.run(MODEL, ARGS, idempotency_key=key, timeout=0.3)


async def test_async_mixed_resend_under_one_key_starts_one_generation(server, fast_poll):
    server.state.model_run_handoff = True
    key = "mixed-resend-key-0002"
    async with _async_client() as client:
        first = await client.models.run(MODEL, ARGS, idempotency_key=key, respond_async=True)
        second = await client.models.run(MODEL, ARGS, idempotency_key=key)
    assert first == second == server.state.queue_result
    assert server.state.model_run_prefer_headers == ["respond-async", None]
    assert server.state.model_run_generations == 1


# --- Preference-Applied parsing ----------------------------------------------


@pytest.mark.parametrize(
    ("value", "applied"),
    [
        ("respond-async", True),
        ("Respond-Async", True),
        ("wait=10, respond-async", True),
        ("respond-async; foo=bar", True),
        ("return=minimal", False),
        ("respond-asynchronously", False),
        ('foo="x, respond-async; y"', False),
        ('foo="a\\"b, respond-async", respond-async', True),
        ("", False),
    ],
)
def test_preference_applied_parsing(value: str, applied: bool):
    assert _preference_applied({"Preference-Applied": value}) is applied


def test_preference_applied_absent():
    headers: dict[str, Any] = {}
    assert _preference_applied(headers) is False


def test_run_handoff_is_not_a_dict():
    # A handle must never be mistakable for a partner's native dict output.
    assert not isinstance(RunHandoff(handle={"request_id": "r"}), dict)


@pytest.mark.parametrize("body", [b"[]", b"null", b'"handle"', b"7"])
def test_non_object_202_is_invalid_response(body: bytes):
    prepared = _Prepared("http://example.invalid", None)
    resp = httpx.Response(202, content=body, headers={"Content-Type": "application/json"})
    with pytest.raises(ApiError) as excinfo:
        prepared.parse_run_outcome(resp)
    assert excinfo.value.code == "invalid_response"
