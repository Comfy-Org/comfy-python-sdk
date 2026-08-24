"""``client.models.run(model, arguments)`` — one call, awaits completion.

Covers the whole contract of the headline model API on both clients: the sync
call blocks and returns the finished result, the async call awaits to the same
shape, the awaitable form is the *async client* rather than a suffixed method
(asserted, not merely absent), the wait is sized for a server that polls
upstream inside the call, an ``Idempotency-Key`` is plumbed onto the wire, and
the result handed back is the provider's own payload rather than a wrapper.

Everything here runs against the stubbed server in ``conftest.py``.
"""

from __future__ import annotations

import inspect
import re

import httpx
import pytest

from comfy_low.transport import MODEL_RUN_TIMEOUT, AsyncComfyLow, ComfyLow, model_run_request
from comfy_sdk import NO_RETRY, AsyncComfy, Comfy
from comfy_sdk.exceptions import ComfyError, NotFound, Unauthorized
from comfy_sdk.models import AsyncModels, Models

MODEL = "acme/flux/dev"
ARGS = {"prompt": "a cat", "steps": 4}


# --- the result of a completed generation -------------------------------


def test_run_returns_the_completed_result(server) -> None:
    with Comfy() as client:
        result = client.models.run(MODEL, ARGS)
    assert result == server.state.model_run_result
    assert server.state.model_run_count == 1


async def test_async_run_is_awaitable_and_returns_the_same_shape(server) -> None:
    async with AsyncComfy() as client:
        result = await client.models.run(MODEL, ARGS)
    assert result == server.state.model_run_result


async def test_both_clients_return_an_identical_result(server) -> None:
    with Comfy() as client:
        sync_result = client.models.run(MODEL, ARGS)
    async with AsyncComfy() as client:
        async_result = await client.models.run(MODEL, ARGS)
    assert sync_result == async_result


def test_the_result_is_the_providers_native_payload_not_a_wrapper(server) -> None:
    # The provider's own field names reach the caller untouched — no SDK class
    # in between, nothing renamed, nothing dropped.
    payload = {"video": {"url": "http://example.invalid/v.mp4"}, "nsfw": False}
    server.state.model_run_result = payload
    with Comfy() as client:
        result = client.models.run(MODEL, ARGS)
    assert type(result) is dict
    assert result == payload


def test_a_created_shaped_success_is_also_a_result(server) -> None:
    server.state.model_run_status = 201
    with Comfy() as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result


def test_run_sends_the_model_and_arguments(server) -> None:
    with Comfy() as client:
        client.models.run(MODEL, ARGS)
    assert server.state.last_model_run_body == {"model": MODEL, "arguments": ARGS}


def test_run_accepts_any_mapping_and_does_not_alias_the_callers_object(server) -> None:
    from types import MappingProxyType

    caller_args = {"prompt": "a dog"}
    with Comfy() as client:
        client.models.run(MODEL, MappingProxyType(caller_args))
    assert server.state.last_model_run_body == {"model": MODEL, "arguments": {"prompt": "a dog"}}
    _path, body, _headers = model_run_request(MODEL, caller_args, None)
    body["arguments"]["prompt"] = "mutated"
    assert caller_args == {"prompt": "a dog"}


# --- the awaitable form is AsyncClient, not a run_async() suffix ---------

_SUFFIXED = re.compile(r"(^async_|_async$|_sync$)")
_SURFACE = [Comfy, AsyncComfy, Models, AsyncModels, ComfyLow, AsyncComfyLow]


@pytest.mark.parametrize("cls", _SURFACE, ids=lambda c: c.__name__)
def test_no_run_async_method_exists(cls: type) -> None:
    # Asserted rather than left to absence: one async mechanism (the async
    # client) is a decided, published contract, and a second name for the same
    # operation cannot be withdrawn once it ships.
    assert not hasattr(cls, "run_async")
    assert not hasattr(cls, "arun")


@pytest.mark.parametrize("cls", _SURFACE, ids=lambda c: c.__name__)
def test_no_suffixed_async_variant_on_the_public_surface(cls: type) -> None:
    # Generalizes the rule past `run`: no public method on either client may
    # signal sync-vs-async in its *name*. (`aclose` is the one deliberate
    # rename and does not match — see tests/test_sync_async_parity.py.)
    offenders = [n for n in dir(cls) if not n.startswith("_") and _SUFFIXED.search(n)]
    assert offenders == [], f"{cls.__name__} exposes suffixed async variants: {offenders}"


def test_run_is_blocking_on_the_sync_client_and_awaitable_on_the_async_one() -> None:
    assert not inspect.iscoroutinefunction(Models.run)
    assert inspect.iscoroutinefunction(AsyncModels.run)


def test_both_clients_spell_the_operation_the_same_way() -> None:
    # Not an exact-contents assertion — later model operations are expected to
    # land here. What must hold is that the two namespaces name the *same*
    # operations, which is the whole point of there being no suffixed variant.
    def names(cls: type) -> set[str]:
        return {n for n, v in vars(cls).items() if not n.startswith("_") and callable(v)}

    assert names(Models) == names(AsyncModels)
    assert "run" in names(Models)


# --- the wait is sized for in-call polling ------------------------------


def test_the_default_timeout_is_minutes_not_tens_of_seconds() -> None:
    assert isinstance(MODEL_RUN_TIMEOUT, httpx.Timeout)
    assert MODEL_RUN_TIMEOUT.read is not None and MODEL_RUN_TIMEOUT.read >= 120
    # Connecting is not generating: an unreachable host still fails fast.
    assert MODEL_RUN_TIMEOUT.connect is not None and MODEL_RUN_TIMEOUT.connect <= 30


def test_a_run_outlives_the_clients_own_timeout(server) -> None:
    # The client is configured for ordinary API calls; the run holds the
    # connection past that and must still complete.
    server.state.model_run_delay = 0.75
    with Comfy(timeout=0.2) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result


async def test_an_async_run_outlives_the_clients_own_timeout(server) -> None:
    server.state.model_run_delay = 0.75
    async with AsyncComfy(timeout=0.2) as client:
        assert await client.models.run(MODEL, ARGS) == server.state.model_run_result


def test_the_clients_own_timeout_would_have_aborted_that_run(server) -> None:
    # Control for the two tests above: without the run-sized default, a 0.2s
    # client really does abort at 0.75s — so they are proving the override,
    # not a stub that answers instantly.
    server.state.model_run_delay = 0.75
    with Comfy(timeout=0.2) as client:
        with pytest.raises(httpx.TimeoutException):
            client.models.run(MODEL, ARGS, timeout=0.2)


def test_an_explicit_timeout_overrides_the_default(server) -> None:
    server.state.model_run_delay = 0.5
    with Comfy() as client:
        with pytest.raises(httpx.TimeoutException):
            client.models.run(MODEL, ARGS, timeout=0.05)


# --- Idempotency-Key -----------------------------------------------------


def test_run_sends_an_idempotency_key(server) -> None:
    with Comfy() as client:
        client.models.run(MODEL, ARGS)
    (key,) = server.state.model_run_idempotency_keys
    assert key


async def test_async_run_sends_an_idempotency_key(server) -> None:
    async with AsyncComfy() as client:
        await client.models.run(MODEL, ARGS)
    (key,) = server.state.model_run_idempotency_keys
    assert key


def test_each_run_mints_a_fresh_key(server) -> None:
    # A second run is a second generation, not a retry of the first.
    with Comfy() as client:
        client.models.run(MODEL, ARGS)
        client.models.run(MODEL, ARGS)
    first, second = server.state.model_run_idempotency_keys
    assert first and second and first != second


def test_an_explicit_key_is_sent_verbatim(server) -> None:
    with Comfy() as client:
        client.models.run(MODEL, ARGS, idempotency_key="caller-chosen-01")
    assert server.state.model_run_idempotency_keys == ["caller-chosen-01"]


async def test_an_explicit_key_is_sent_verbatim_on_the_async_client(server) -> None:
    async with AsyncComfy() as client:
        await client.models.run(MODEL, ARGS, idempotency_key="caller-chosen-02")
    assert server.state.model_run_idempotency_keys == ["caller-chosen-02"]


# --- the shared client configuration still applies -----------------------


def test_run_carries_the_host_clients_credentials(server) -> None:
    server.state.require_auth = True
    with Comfy(api_key="k-run") as client:
        client.models.run(MODEL, ARGS)
    assert server.state.last_auth_header == "Bearer k-run"


# --- errors stay on the SDK's own surface --------------------------------


def test_a_failing_run_raises_the_sdk_exception_not_the_protocol_one(server) -> None:
    server.state.model_run_error = (404, "not_found")
    with Comfy() as client:
        with pytest.raises(NotFound):
            client.models.run(MODEL, ARGS)


async def test_a_failing_async_run_raises_the_sdk_exception(server) -> None:
    server.state.model_run_error = (401, "unauthorized")
    async with AsyncComfy() as client:
        with pytest.raises(Unauthorized):
            await client.models.run(MODEL, ARGS)


def test_an_unmapped_failure_still_lands_as_a_comfy_error(server) -> None:
    # `NO_RETRY` because a 503 is retryable: under the default policy this
    # would spend the whole retry budget re-asking a permanently-failing stub
    # before raising. What is under test here is the mapping, not the policy —
    # tests/test_models_run_retry.py covers the retrying half.
    server.state.model_run_error = (503, "model_unavailable")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.code == "model_unavailable"
    assert excinfo.value.http_status == 503
