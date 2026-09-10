"""The queued model surface: ``models.submit`` / ``subscribe`` / ``handle``.

Driven against the recorded stub in ``conftest.py``, never a live deployment —
``server.state`` is set to the queue scenario under test and the SDK is pointed
at the stub by the fixture.

What each group here is for:

* the **routes** the four operations address, and the encoding of the ids in
  them, since a queued request is addressed by model id *and* request id;
* **poll-authoritative** completion — adaptive backoff, and the server's own
  ``Retry-After`` beating it when it names one;
* the **completion-is-not-a-success** rule: a ``COMPLETED`` carrying an
  ``error_type`` raises the typed router exception from every path that hands
  back a result, whether the bucket arrives on the status or on the result;
* the **Idempotency-Key** contract — one fresh key per ``submit`` call;
* ``subscribe``'s client-side timeout, which cancels before it raises;
* and that ``models.run`` is untouched by all of it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from comfy_low.transport import (
    _MODEL_REQUEST_CANCEL_PATH_TEMPLATE,
    _MODEL_REQUEST_PATH_TEMPLATE,
    _MODEL_REQUEST_STATUS_PATH_TEMPLATE,
    _MODEL_REQUESTS_PATH_TEMPLATE,
    _MODEL_RUN_PATH_TEMPLATE,
)
from comfy_sdk import AsyncComfy, Comfy, QueueUpdate
from comfy_sdk.exceptions import ComfyError
from comfy_sdk.model_requests import COMPLETED, AsyncRequestHandle, RequestHandle
from comfy_sdk.retry import NO_RETRY
from comfy_sdk.router_exceptions import (
    ContentPolicyViolation,
    NotEnabled,
    RouterError,
    error_from_completion,
)

MODEL = "acme/fast-sdxl"
ARGS = {"prompt": "a red bicycle"}


@pytest.fixture
def fast_poll(monkeypatch):
    """Collapse the poll backoff so a multi-poll test is not a multi-second one.

    Patched on ``comfy_sdk._core`` — the one place the schedule is defined —
    rather than on the handles, so both the sync and the async loop pick it up
    and neither can drift onto a second schedule.
    """
    import comfy_sdk._core as core

    monkeypatch.setattr(core, "backoff_schedule", lambda *a, **k: iter(lambda: 0.0, None))
    return None


def _client(**kw: Any) -> Comfy:
    return Comfy(api_key="comfyui-test-key", **kw)


# --- routes ----------------------------------------------------------------


def test_submit_posts_to_the_requests_route_under_the_model_id(server, fast_poll) -> None:
    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)

    assert isinstance(handle, RequestHandle)
    assert handle.request_id == server.state.queue_request_id
    assert handle.model == MODEL
    assert server.state.queue_paths == ["/v2/models/acme/fast-sdxl/requests"]
    # The partner model's native input, with no Comfy-shaped envelope — the
    # same body `models.run` sends.
    assert server.state.last_queue_submit_body == ARGS
    assert (server.state.last_queue_provider, server.state.last_queue_model) == (
        "acme",
        "fast-sdxl",
    )


def test_every_queue_route_is_addressed_by_both_ids(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 0
    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        handle.status()
        handle.get()
        handle.cancel()

    prefix = "/v2/models/acme/fast-sdxl/requests"
    rid = server.state.queue_request_id
    assert server.state.queue_paths == [
        prefix,
        f"{prefix}/{rid}/status",
        f"{prefix}/{rid}/status",
        f"{prefix}/{rid}",
        f"{prefix}/{rid}/cancel",
    ]


def test_the_ids_are_percent_encoded_into_the_path(server, fast_poll) -> None:
    server.state.queue_request_id = "req id?x=1"
    with _client() as client:
        handle = client.models.submit("acme/model name", ARGS)
        handle.status()

    assert server.state.queue_paths[0] == "/v2/models/acme/model%20name/requests"
    # Nothing in either id can add a segment, a query or a fragment.
    assert server.state.queue_paths[1].endswith("/requests/req%20id%3Fx%3D1/status")


def test_a_server_named_request_id_that_would_walk_the_path_is_refused(server) -> None:
    """A hostile or broken id fails at the submit, not three calls later."""
    server.state.queue_request_id = "../escape"
    with _client() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.submit(MODEL, ARGS)

    assert "cannot address a route" in str(excinfo.value)
    assert excinfo.value.idempotency_key, "the accepted request stays recoverable"


@pytest.mark.parametrize(
    "model,expected",
    [("one-segment", ValueError), ("a/b/c", ValueError), (object(), TypeError)],
)
def test_submit_rejects_a_malformed_model_id_before_any_request(server, model, expected) -> None:
    with _client() as client:
        with pytest.raises(expected):
            client.models.submit(model, ARGS)
    assert server.state.queue_submit_count == 0


@pytest.mark.parametrize(
    "request_id,expected",
    [("", ValueError), ("a/b", ValueError), ("..", ValueError), (object(), TypeError)],
)
def test_handle_rejects_a_malformed_request_id_before_any_request(
    server, request_id, expected
) -> None:
    with _client() as client:
        with pytest.raises(expected):
            client.models.handle(MODEL, request_id)
    assert server.state.queue_status_count == 0


def test_handle_rehydrates_from_the_two_ids_without_a_request(server, fast_poll) -> None:
    """The other-process case: no submit here, only the ids."""
    with _client() as client:
        handle = client.models.handle(MODEL, "req_from_elsewhere")
        assert (handle.model, handle.request_id) == (MODEL, "req_from_elsewhere")
        # Constructing it made no call at all — the first one is the poll.
        assert server.state.queue_status_count == 0
        update = handle.status()

    assert server.state.queue_status_count == 1
    assert update.request_id == "req_from_elsewhere"


def test_a_submit_whose_response_names_no_request_id_is_an_error(server) -> None:
    server.state.queue_submit_omits_request_id = True
    with _client() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.submit(MODEL, ARGS)
    assert "request_id" in str(excinfo.value)
    # The key is still reachable, because the work may have been accepted.
    assert excinfo.value.idempotency_key


# --- poll-authoritative completion ------------------------------------------


def test_get_polls_to_completion_then_collects_the_result(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 3
    with _client() as client:
        result = client.models.submit(MODEL, ARGS).get()

    assert result == server.state.queue_result
    # Three pending polls plus the completing one; the result is fetched once.
    assert server.state.queue_status_count == 4
    assert server.state.queue_result_count == 1


def test_iter_events_yields_the_first_state_every_change_and_the_completion(
    server, fast_poll
) -> None:
    server.state.queue_polls_to_complete = 3
    server.state.queue_start_position = 2
    with _client() as client:
        updates = list(client.models.submit(MODEL, ARGS).iter_events())

    assert [u.status for u in updates] == ["IN_QUEUE", "IN_QUEUE", "IN_QUEUE", COMPLETED]
    # Positions 2, 1, 0 — the server's numbers, never computed locally. The
    # third pending poll repeats position 0 and is therefore not re-reported.
    assert [u.queue_position for u in updates] == [2, 1, 0, None]
    assert updates[-1].is_completed


def test_an_unchanged_poll_is_not_re_reported(server, fast_poll) -> None:
    """A queue that has not moved must not redraw the caller's progress bar."""
    server.state.queue_polls_to_complete = 4
    server.state.queue_start_position = 0  # every pending poll reports position 0
    with _client() as client:
        updates = list(client.models.submit(MODEL, ARGS).iter_events())

    assert [u.status for u in updates] == ["IN_QUEUE", COMPLETED]
    assert server.state.queue_status_count == 5


def test_an_unknown_status_is_not_treated_as_terminal(server, fast_poll) -> None:
    """A status this version has never heard of keeps polling rather than
    collecting a result that does not exist yet."""
    server.state.queue_pending_status = "SOME_FUTURE_STATE"
    server.state.queue_polls_to_complete = 2
    with _client() as client:
        result = client.models.submit(MODEL, ARGS).get()

    assert result == server.state.queue_result
    assert server.state.queue_status_count == 3


def test_a_server_named_retry_after_paces_the_poll(server, monkeypatch) -> None:
    """``Retry-After`` on a *successful* poll beats the local backoff."""
    slept: list[float] = []
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", slept.append)
    server.state.queue_polls_to_complete = 2
    server.state.queue_status_retry_after = "7"

    with _client() as client:
        client.models.submit(MODEL, ARGS).get()

    # One sleep per pending poll, each at the server's pace rather than the
    # 0.5s the adaptive schedule would have started from.
    assert slept == [7.0, 7.0]


def test_a_useless_retry_after_falls_back_to_the_backoff(server, monkeypatch) -> None:
    """``Retry-After: 0`` names no pace and must not become a zero-delay loop."""
    slept: list[float] = []
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", slept.append)
    server.state.queue_polls_to_complete = 2
    server.state.queue_status_retry_after = "0"

    with _client() as client:
        client.models.submit(MODEL, ARGS).get()

    assert slept and all(delay > 0 for delay in slept)


def test_a_throttled_poll_is_retried_under_the_client_policy(
    server, fast_poll, monkeypatch
) -> None:
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    server.state.queue_polls_to_complete = 0
    server.state.queue_status_fail_times = 2  # 429 rate_limited, twice

    with _client() as client:
        result = client.models.submit(MODEL, ARGS).get()

    assert result == server.state.queue_result
    # Two throttled polls plus the one that answered — the throttled ones did
    # not advance the queue, and did not surface to the caller.
    assert server.state.queue_status_count == 3
    assert server.state.queue_status_served == 1


def test_a_throttled_poll_raises_when_the_client_does_not_retry(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 0
    server.state.queue_status_fail_times = 1

    with _client(retry=NO_RETRY) as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises(RouterError) as excinfo:
            handle.status()
    assert excinfo.value.error_type == "rate_limited"


# --- a completion is not a success ------------------------------------------


def test_a_completed_status_carrying_an_error_type_raises_the_typed_error(
    server, fast_poll
) -> None:
    server.state.queue_polls_to_complete = 1
    server.state.queue_error_type = "content_policy_violation"
    server.state.queue_error_detail = "the prompt was refused"

    with _client() as client:
        with pytest.raises(ContentPolicyViolation) as excinfo:
            client.models.submit(MODEL, ARGS).get()

    assert excinfo.value.error_type == "content_policy_violation"
    assert excinfo.value.detail == "the prompt was refused"
    assert excinfo.value.request_id == server.state.queue_request_id
    # The result was never collected: the failure was already known.
    assert server.state.queue_result_count == 0


def test_an_error_type_on_the_result_body_alone_still_raises(server, fast_poll) -> None:
    """The other half of the rule — whichever response carries the bucket."""
    server.state.queue_polls_to_complete = 0
    server.state.queue_result_error_type = "provider_error"

    with _client() as client:
        with pytest.raises(RouterError) as excinfo:
            client.models.submit(MODEL, ARGS).get()

    assert excinfo.value.error_type == "provider_error"
    assert server.state.queue_result_count == 1


def test_a_providers_own_error_type_field_is_not_mistaken_for_a_failure(server, fast_poll) -> None:
    """The result body is the provider's native output, forwarded verbatim.

    A partner model is free to have a field called ``error_type`` in its own
    schema. Raising on one would fail a generation that succeeded, so on the
    result route the bucket only counts inside the queue's own envelope — which
    the ``COMPLETED`` status alongside it is what identifies.
    """
    server.state.queue_polls_to_complete = 0
    server.state.queue_result_extra = {"error_type": "provider_error"}

    with _client() as client:
        result = client.models.submit(MODEL, ARGS).get()

    assert result["error_type"] == "provider_error"
    assert result["seed"] == server.state.queue_result["seed"]


def test_an_unknown_error_type_still_raises_the_base_router_error(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 0
    server.state.queue_error_type = "a_bucket_from_the_future"

    with _client() as client:
        with pytest.raises(RouterError) as excinfo:
            client.models.submit(MODEL, ARGS).get()

    assert type(excinfo.value) is RouterError
    assert excinfo.value.error_type == "a_bucket_from_the_future"


def test_iter_events_reports_the_failure_as_data_rather_than_raising(server, fast_poll) -> None:
    """The split that makes ``iter_events`` a view and ``get`` the collector."""
    server.state.queue_polls_to_complete = 0
    server.state.queue_error_type = "provider_error"

    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        updates = list(handle.iter_events())
        assert updates[-1].error_type == "provider_error"
        with pytest.raises(RouterError):
            handle.get()


def test_a_clean_completion_reports_no_error() -> None:
    assert error_from_completion({"status": COMPLETED, "request_id": "r"}) is None
    assert error_from_completion(None) is None
    assert error_from_completion({"error_type": "  "}) is None


def test_a_request_the_server_does_not_know_surfaces_as_the_servers_own_answer(
    server, fast_poll
) -> None:
    """``handle`` makes no call, so an id that names nothing fails on the poll."""
    server.state.queue_status_error = (404, "model_not_found")

    with _client() as client:
        handle = client.models.handle(MODEL, "req_that_never_existed")
        with pytest.raises(RouterError) as excinfo:
            handle.status()

    assert excinfo.value.http_status == 404
    assert excinfo.value.error_type == "model_not_found"


def test_an_unflagged_caller_gets_the_servers_not_enabled_as_the_typed_error(server) -> None:
    """The surface is gated server side; the SDK's job is to type the refusal."""
    server.state.queue_submit_error = (403, "not_enabled")

    with _client() as client:
        with pytest.raises(NotEnabled) as excinfo:
            client.models.submit(MODEL, ARGS)

    assert excinfo.value.error_type == "not_enabled"
    assert excinfo.value.http_status == 403


# --- the Idempotency-Key contract -------------------------------------------


def test_each_submit_call_mints_a_fresh_key(server, fast_poll) -> None:
    with _client() as client:
        client.models.submit(MODEL, ARGS)
        client.models.submit(MODEL, ARGS)

    keys = server.state.queue_submit_idempotency_keys
    assert len(keys) == 2
    assert all(keys)
    assert keys[0] != keys[1], "two deliberate submits must be two requests, not one"


def test_a_transport_retry_of_one_submit_keeps_the_one_key(server, monkeypatch) -> None:
    """The other half of the rule: one *call* is one key, however many attempts.

    A fresh key per attempt would bill a retried submit as a second queued
    generation, which is the failure the one-key rule exists to prevent.
    """
    monkeypatch.setattr("comfy_sdk.models.time.sleep", lambda _s: None)
    server.state.queue_submit_fail_times = 2  # two 429s, then accepted

    with _client() as client:
        client.models.submit(MODEL, ARGS)

    keys = server.state.queue_submit_idempotency_keys
    assert len(keys) == 3, "three attempts should have reached the server"
    assert len(set(keys)) == 1, "every attempt of one call carries one key"


def test_an_explicit_key_is_used_verbatim(server, fast_poll) -> None:
    with _client() as client:
        client.models.submit(MODEL, ARGS, idempotency_key="my-own-key-01")
    assert server.state.queue_submit_idempotency_keys == ["my-own-key-01"]


def test_an_unusable_explicit_key_is_refused_locally(server) -> None:
    with _client() as client:
        with pytest.raises(ValueError):
            client.models.submit(MODEL, ARGS, idempotency_key="")
    assert server.state.queue_submit_count == 0


def test_the_key_rides_out_on_a_failure(server) -> None:
    server.state.queue_submit_error = (400, "invalid_input")
    with _client(retry=NO_RETRY) as client:
        with pytest.raises(RouterError) as excinfo:
            client.models.submit(MODEL, ARGS, idempotency_key="key-for-recovery")
    assert excinfo.value.idempotency_key == "key-for-recovery"


# --- cancel and subscribe ---------------------------------------------------


def test_cancel_asks_the_server_and_reports_what_it_said(server, fast_poll) -> None:
    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        update = handle.cancel()

    assert server.state.queue_cancel_count == 1
    assert update.request_id == server.state.queue_request_id
    assert update.error_type == server.state.queue_cancel_error_type


def test_a_cancel_answered_with_no_body_still_identifies_the_request(server, fast_poll) -> None:
    server.state.queue_cancel_status = 204
    with _client() as client:
        update = client.models.submit(MODEL, ARGS).cancel()

    assert update.request_id == server.state.queue_request_id
    assert update.status == ""


def test_subscribe_reports_progress_and_returns_the_result(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 2
    seen: list[QueueUpdate] = []

    with _client() as client:
        result = client.models.subscribe(MODEL, ARGS, on_queue_update=seen.append)

    assert result == server.state.queue_result
    assert [u.status for u in seen] == ["IN_QUEUE", "IN_QUEUE", COMPLETED]


def test_subscribe_needs_no_callback(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 1
    with _client() as client:
        assert client.models.subscribe(MODEL, ARGS) == server.state.queue_result


def test_subscribe_raises_the_typed_error_for_a_failed_completion(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 0
    server.state.queue_error_type = "content_policy_violation"
    with _client() as client:
        with pytest.raises(ContentPolicyViolation):
            client.models.subscribe(MODEL, ARGS)


def test_subscribes_timeout_cancels_before_it_raises(server, monkeypatch) -> None:
    """Acceptance: a caller who has stopped waiting is not still paying."""
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    # Never completes on its own.
    server.state.queue_polls_to_complete = 10_000

    with _client() as client:
        with pytest.raises(TimeoutError):
            client.models.subscribe(MODEL, ARGS, timeout=0.0)

    assert server.state.queue_cancel_count == 1


def test_a_failing_cancel_does_not_mask_the_timeout(server, monkeypatch) -> None:
    """Best-effort is literal: the timeout is the failure worth reporting."""
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    server.state.queue_polls_to_complete = 10_000

    def _explode(self: Any) -> None:
        raise ComfyError("cancel is unreachable")

    monkeypatch.setattr(RequestHandle, "_cancel_best_effort", _explode)

    with _client() as client:
        with pytest.raises(TimeoutError):
            client.models.subscribe(MODEL, ARGS, timeout=0.0)


def test_a_callbacks_own_timeout_error_does_not_cancel_the_request(server, monkeypatch) -> None:
    """The caller's callback is the caller's code, and its failures are its own.

    A progress callback that makes its own HTTP call can raise ``TimeoutError``
    for reasons that have nothing to do with this wait; cancelling a healthy
    queued request on the strength of it would be a charge thrown away.
    """
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    server.state.queue_polls_to_complete = 3

    def _explodes(_update: QueueUpdate) -> None:
        raise TimeoutError("the callback's own HTTP call timed out")

    with _client() as client:
        with pytest.raises(TimeoutError, match="callback"):
            client.models.subscribe(MODEL, ARGS, on_queue_update=_explodes)

    assert server.state.queue_cancel_count == 0


def test_subscribe_collects_without_a_second_status_poll(server, fast_poll) -> None:
    """It has already polled its way to the completion; re-discovering it is
    one request spent on something it is holding."""
    server.state.queue_polls_to_complete = 2

    with _client() as client:
        client.models.subscribe(MODEL, ARGS)

    # Two pending polls plus the completing one — and no fourth.
    assert server.state.queue_status_count == 3
    assert server.state.queue_result_count == 1


def test_iter_events_does_not_cancel_on_its_own_timeout(server, monkeypatch) -> None:
    """A ``for`` loop over the queue must not be destructive."""
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    server.state.queue_polls_to_complete = 10_000

    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises(TimeoutError):
            list(handle.iter_events(timeout=0.0))

    assert server.state.queue_cancel_count == 0


def test_a_timeout_never_sleeps_past_the_deadline(server, monkeypatch) -> None:
    """A long server-named pace must not outlive the caller's own bound."""
    slept: list[float] = []
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", slept.append)
    server.state.queue_polls_to_complete = 10_000
    server.state.queue_status_retry_after = "600"

    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            list(handle.iter_events(timeout=0.25))

    assert slept, "the loop should have paced at least one poll"
    assert max(slept) <= 0.25, f"slept past the caller's deadline: {slept}"
    assert time.monotonic() - started < 5


# --- the routes as constants ------------------------------------------------


def test_the_queue_routes_extend_the_run_route() -> None:
    """The four queue routes hang off the model-run path, in one place.

    The Router contract that declares them is authored but held, so unlike
    ``_MODEL_RUN_PATH_TEMPLATE`` there is no vendored spec to pin them against
    (``tests/test_router_spec_contract.py`` does that for the run path). What
    can be pinned is the relationship: they are the same model-ID-addressed
    prefix plus a ``requests`` collection, and each fills exactly the segments
    it declares. When the operations land in ``spec/router-openapi.yaml`` this
    is the assertion to replace with a comparison against the file.
    """
    assert _MODEL_REQUESTS_PATH_TEMPLATE == _MODEL_RUN_PATH_TEMPLATE + "/requests"
    assert _MODEL_REQUEST_PATH_TEMPLATE == _MODEL_REQUESTS_PATH_TEMPLATE + "/{request_id}"
    for template in (
        _MODEL_REQUEST_PATH_TEMPLATE,
        _MODEL_REQUEST_STATUS_PATH_TEMPLATE,
        _MODEL_REQUEST_CANCEL_PATH_TEMPLATE,
    ):
        assert template.count("{") == 3
        assert all(part in template for part in ("{provider}", "{model}", "{request_id}"))


# --- models.run is untouched ------------------------------------------------


def test_run_still_posts_to_its_own_route_and_returns_the_payload(server) -> None:
    """Acceptance: the queued surface must not have moved ``run`` an inch."""
    with _client() as client:
        result = client.models.run(MODEL, ARGS)

    assert result == server.state.model_run_result
    assert server.state.last_model_run_path == "/v2/models/acme/fast-sdxl"
    assert server.state.queue_submit_count == 0
    assert server.state.queue_status_count == 0


# --- the async client -------------------------------------------------------


async def test_async_submit_and_get(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 2
    async with AsyncComfy(api_key="comfyui-test-key") as client:
        handle = await client.models.submit(MODEL, ARGS)
        assert isinstance(handle, AsyncRequestHandle)
        assert await handle.get() == server.state.queue_result


async def test_async_iter_events(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 2
    async with AsyncComfy(api_key="comfyui-test-key") as client:
        handle = await client.models.submit(MODEL, ARGS)
        statuses = [update.status async for update in handle.iter_events()]
    assert statuses == ["IN_QUEUE", "IN_QUEUE", COMPLETED]


async def test_async_handle_rehydrates_without_a_request(server, fast_poll) -> None:
    async with AsyncComfy(api_key="comfyui-test-key") as client:
        handle = await client.models.handle(MODEL, "req_elsewhere")
        assert server.state.queue_status_count == 0
        update = await handle.status()
    assert update.request_id == "req_elsewhere"


async def test_async_subscribe_awaits_an_async_callback(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 1
    seen: list[str] = []

    async def _record(update: QueueUpdate) -> None:
        await asyncio.sleep(0)
        seen.append(update.status)

    async with AsyncComfy(api_key="comfyui-test-key") as client:
        result = await client.models.subscribe(MODEL, ARGS, on_queue_update=_record)

    assert result == server.state.queue_result
    assert seen == ["IN_QUEUE", COMPLETED]


async def test_async_subscribe_timeout_cancels_before_raising(server, monkeypatch) -> None:
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("comfy_sdk.model_requests.asyncio.sleep", _no_sleep)
    server.state.queue_polls_to_complete = 10_000

    async with AsyncComfy(api_key="comfyui-test-key") as client:
        with pytest.raises(TimeoutError):
            await client.models.subscribe(MODEL, ARGS, timeout=0.0)

    assert server.state.queue_cancel_count == 1


async def test_async_completion_error_raises_the_typed_exception(server, fast_poll) -> None:
    server.state.queue_polls_to_complete = 0
    server.state.queue_error_type = "content_policy_violation"
    async with AsyncComfy(api_key="comfyui-test-key") as client:
        handle = await client.models.submit(MODEL, ARGS)
        with pytest.raises(ContentPolicyViolation):
            await handle.get()


# --- review follow-ups: bounded waits, malformed bodies, the cleanup cancel ---


def test_a_blank_error_type_reads_as_no_error_on_an_update() -> None:
    """The update and the raising path read ``error_type`` the same way."""
    import httpx

    from comfy_sdk.model_requests import _update_from

    body = {"status": COMPLETED, "error_type": "  "}
    update = _update_from(body, httpx.Headers(), request_id="r")

    assert update.error_type is None
    assert error_from_completion(body) is None


def test_an_update_carries_the_id_it_was_addressed_by() -> None:
    import httpx

    from comfy_sdk.model_requests import _update_from

    body = {"request_id": "somebody-else\n", "status": "IN_QUEUE"}
    update = _update_from(body, httpx.Headers(), request_id="mine")

    assert update.request_id == "mine"
    assert update.raw == body


def test_a_result_that_is_not_a_json_object_is_returned_unchanged(server, fast_poll) -> None:
    """The result is the partner's document, whatever shape the partner gave it."""
    server.state.queue_polls_to_complete = 0
    server.state.queue_result_raw = [{"url": "http://example.invalid/a.png"}]

    with _client() as client:
        assert client.models.submit(MODEL, ARGS).get() == server.state.queue_result_raw


def test_a_status_read_naming_no_status_is_an_invalid_response(server, fast_poll) -> None:
    """A ``200 {}`` from the authoritative read must not poll forever."""
    server.state.queue_status_omits_status = True

    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises(ComfyError) as excinfo:
            handle.get()

    assert excinfo.value.code == "invalid_response"


def test_a_huge_retry_after_is_capped_before_it_is_slept(server, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", slept.append)
    server.state.queue_polls_to_complete = 1
    # Parses as an int; `float()` of it would overflow.
    server.state.queue_status_retry_after = "9" * 400

    with _client() as client:
        client.models.submit(MODEL, ARGS).get()

    assert slept == [60.0]


def test_a_timeout_bounds_the_poll_and_its_retries_not_only_the_sleep(server, monkeypatch) -> None:
    """``get(timeout=...)`` on a server that keeps throttling returns within the
    bound instead of riding the retry policy's whole minute."""
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    server.state.queue_status_fail_times = 10_000

    started = time.monotonic()
    with _client() as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises((RouterError, TimeoutError)):
            handle.get(timeout=0.5)

    assert time.monotonic() - started < 5


def test_the_cleanup_cancel_after_a_timeout_does_not_ride_the_retry_policy(
    server, monkeypatch
) -> None:
    monkeypatch.setattr("comfy_sdk.model_requests.time.sleep", lambda _s: None)
    server.state.queue_polls_to_complete = 10_000
    # Every cancel is answered with a paced 429, which the full policy would
    # retry for the whole of its budget.
    server.state.queue_cancel_fail_times = 10_000

    started = time.monotonic()
    with _client() as client:
        with pytest.raises(TimeoutError):
            client.models.subscribe(MODEL, ARGS, timeout=0.0)

    assert server.state.queue_cancel_count == 1
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("request_id", ["abc\n", "with\x00nul", "x" * 257])
def test_handle_refuses_an_unprintable_or_oversized_request_id(server, request_id) -> None:
    with _client() as client:
        with pytest.raises(ValueError):
            client.models.handle(MODEL, request_id)


def test_cancel_is_a_put(server, fast_poll) -> None:
    """The contract's cancel is ``PUT``; a POST would be the wrong verb."""
    with _client() as client:
        client.models.submit(MODEL, ARGS).cancel()

    assert server.state.queue_cancel_methods == ["PUT"]


async def test_async_subscribe_cancellation_requests_a_remote_cancel(server, monkeypatch) -> None:
    """A task cancelled from outside still asks the server to stop the run.

    The cancel is delivered while the loop is in its own pause between polls,
    so the test exercises this SDK's handling of the cancellation rather than
    the HTTP stack's: a ``Task.cancel()`` that lands inside an in-flight
    request is the transport's to surface, and when it surfaces late the loop
    simply reaches this same pause on its next iteration.
    """
    server.state.queue_polls_to_complete = 10_000
    real_sleep = asyncio.sleep
    pausing = asyncio.Event()

    # `comfy_sdk.model_requests` imports the `asyncio` module itself, so this
    # patch lands on the shared `asyncio.sleep` for the test's duration. It is
    # a pass-through that only *reports* the pause: every caller still waits
    # the delay it asked for, and the cancel below is what cuts the wait short.
    async def _pause(delay: float) -> None:
        pausing.set()
        await real_sleep(delay)

    monkeypatch.setattr("comfy_sdk.model_requests.asyncio.sleep", _pause)

    async with AsyncComfy(api_key="comfyui-test-key") as client:
        task = asyncio.ensure_future(client.models.subscribe(MODEL, ARGS))
        await pausing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert server.state.queue_cancel_count == 1
