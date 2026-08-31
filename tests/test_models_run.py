"""``client.models.run(model, arguments)`` — one call, awaits completion.

Covers the whole contract of the headline model API on both clients: the sync
call blocks and returns the finished result, the async call awaits to the same
shape, the awaitable form is the *async client* rather than a suffixed method
(asserted, not merely absent), the run is addressed to Comfy Router by the
model's two id segments with the model's own native JSON as the body, the
result is that model's native output, the wait is sized for a server that polls
upstream inside the call, an ``Idempotency-Key`` is plumbed onto the wire and rides
out on whatever the call raises, and the result handed back is the provider's
own payload rather than a wrapper.

Everything here runs against the stubbed server in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Mapping
from typing import Any, cast

import httpx
import pytest

from comfy_low.transport import (
    MODEL_RUN_TIMEOUT,
    ROUTER_BASE_URL,
    AsyncComfyLow,
    ComfyLow,
    model_run_request,
    parse_model_id,
)
from comfy_sdk import (
    COMFY_ROUTER_BASE_URL,
    NO_RETRY,
    ROUTER_BASE_URL_ENV_VAR,
    AsyncComfy,
    Comfy,
    RetryPolicy,
)
from comfy_sdk.exceptions import ComfyError, NotFound, Unauthorized
from comfy_sdk.models import AsyncModels, Models
from comfy_sdk.router_exceptions import (
    ERROR_TYPE_HEADER,
    DeadlineExceeded,
    RouterError,
    error_from_response,
)

#: The canonical two-segment ``{provider}/{model}`` id the route is addressed
#: by — the same shape ``spec/router-openapi.yaml``'s ``RouterModelId`` pattern
#: declares, and the id the catalog lists.
MODEL = "acme/flux-dev"
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


# --- the model id addresses the route; the body is the model's own input ----


def test_run_addresses_the_model_by_path_and_sends_the_native_body(server) -> None:
    # The wire shape Router declares: the id is the two path segments of
    # `/v2/models/{provider}/{model}`, and the body is the partner model's OWN
    # native JSON input, forwarded unchanged — no `{model, arguments}` envelope.
    with Comfy() as client:
        client.models.run(MODEL, ARGS)
    assert server.state.last_model_run_path == "/v2/models/acme/flux-dev"
    assert server.state.last_model_run_provider == "acme"
    assert server.state.last_model_run_model == "flux-dev"
    assert server.state.last_model_run_body == ARGS
    assert "model" not in server.state.last_model_run_body
    assert "arguments" not in server.state.last_model_run_body


async def test_the_async_client_addresses_the_route_the_same_way(server) -> None:
    async with AsyncComfy() as client:
        await client.models.run(MODEL, ARGS)
    assert server.state.last_model_run_path == "/v2/models/acme/flux-dev"
    assert server.state.last_model_run_body == ARGS


def test_the_sans_io_request_builder_agrees_with_the_wire() -> None:
    # The one place the wire shape is decided, asserted directly so a change to
    # it cannot hide behind the stub's own routing.
    path, body, headers = model_run_request(MODEL, ARGS, "k-1")
    assert path == "/v2/models/acme/flux-dev"
    assert body == ARGS
    assert headers == {"Idempotency-Key": "k-1"}


@pytest.mark.parametrize(
    "model, path",
    [
        # `.`, `_` and `-` are all legal *inside* a segment per the spec's
        # `RouterModelSegment` pattern, and none of them is percent-encoded:
        # they are unreserved (or sub-delims) in a path segment, so the URL the
        # caller reads in a log is the id they passed.
        ("fal-ai/flux-pro", "/v2/models/fal-ai/flux-pro"),
        ("acme/sd_xl.turbo", "/v2/models/acme/sd_xl.turbo"),
        ("acme_labs/v1.5", "/v2/models/acme_labs/v1.5"),
        # ...while anything that would change the *structure* of the path is
        # encoded rather than passed through.
        ("acme/a b", "/v2/models/acme/a%20b"),
        ("acme/a?b", "/v2/models/acme/a%3Fb"),
        ("acme/a#b", "/v2/models/acme/a%23b"),
    ],
)
def test_each_segment_is_percent_encoded_into_exactly_one_path_segment(
    model: str, path: str
) -> None:
    assert model_run_request(model, {}, None)[0] == path


@pytest.mark.parametrize(
    "bad",
    [
        "flux-dev",  # one segment — no provider
        "acme/flux/dev",  # three — the variant form, not addressable here
        "acme/flux/dev/fp8",  # four
        "acme/..",  # traversal
        "../flux-dev",
        "./flux-dev",
        "acme/.",
        "a//b",  # an empty middle segment
        "/flux-dev",  # empty provider
        "acme/",  # empty model
        "",
        "/",
    ],
)
def test_a_malformed_model_id_is_refused_locally(bad: str) -> None:
    # Refused before any request: the id *is* the path, so a malformed one
    # would otherwise be pasted into a URL and answered by whatever route it
    # landed on — a 404 that looks like "no such model" rather than "you passed
    # a bad id".
    with pytest.raises(ValueError):
        model_run_request(bad, {}, None)
    with pytest.raises(ValueError):
        parse_model_id(bad)


def test_a_three_segment_id_says_the_variant_is_not_addressable_yet() -> None:
    # The message matters: a `{provider}/{model}/{variant}` id is a real id
    # shape, just not one this route takes, and "invalid model id" would send
    # the caller looking for a typo.
    with pytest.raises(ValueError, match="variant"):
        parse_model_id("acme/flux/dev")


def test_a_non_string_model_id_is_a_type_error_not_a_value_error() -> None:
    # Python's own split: a wrong *type* is a programming error, and folding it
    # into ValueError would let `except ValueError` around user input swallow it.
    for bad in (None, 3, ["acme", "flux-dev"]):
        with pytest.raises(TypeError):
            parse_model_id(bad)  # type: ignore[arg-type]


def test_a_malformed_id_never_reaches_the_server(server) -> None:
    with Comfy() as client:
        with pytest.raises(ValueError):
            client.models.run("acme/flux/dev", ARGS)
    assert server.state.model_run_count == 0


def test_run_accepts_any_mapping_and_does_not_alias_the_callers_object(server) -> None:
    from types import MappingProxyType

    caller_args = {"prompt": "a dog"}
    with Comfy() as client:
        client.models.run(MODEL, MappingProxyType(caller_args))
    assert server.state.last_model_run_body == {"prompt": "a dog"}
    _path, body, _headers = model_run_request(MODEL, caller_args, None)
    body["prompt"] = "mutated"
    assert caller_args == {"prompt": "a dog"}


# --- which host the run is addressed to ---------------------------------


def test_the_default_router_base_url_is_the_public_one() -> None:
    assert COMFY_ROUTER_BASE_URL == ROUTER_BASE_URL == "https://api.comfy.org"
    assert ROUTER_BASE_URL_ENV_VAR == "COMFY_ROUTER_BASE_URL"


def test_a_client_defaults_to_the_public_router(monkeypatch) -> None:
    # No `server` fixture here on purpose: that fixture is what points the
    # router at the stub, so this asserts the *unconfigured* default.
    monkeypatch.delenv(ROUTER_BASE_URL_ENV_VAR, raising=False)
    with Comfy(api_key="comfyui-test") as client:
        assert client.models.base_url == "https://api.comfy.org"
        assert client._low.router_base_url == "https://api.comfy.org"


def test_the_router_env_var_redirects_model_runs(monkeypatch, server) -> None:
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, "http://127.0.0.1:9/router")
    with Comfy() as client:
        assert client.models.base_url == "http://127.0.0.1:9/router"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_router_env_var_means_the_default(monkeypatch, blank: str) -> None:
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, blank)
    with Comfy(api_key="comfyui-test") as client:
        assert client.models.base_url == COMFY_ROUTER_BASE_URL


def test_a_trailing_slash_on_the_router_env_var_is_stripped(monkeypatch) -> None:
    # It is concatenated with a path that already starts with `/`, so a kept
    # slash would request `//v2/models/...` — a different path to an origin
    # server than the one the vendored spec declares.
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, "https://router.example/")
    with Comfy(api_key="comfyui-test") as client:
        assert client.models.base_url == "https://router.example"


@pytest.mark.parametrize("bad", ["not-a-url", "ftp://h", "https://h?x=1", "https://h#f"])
def test_a_malformed_router_env_var_is_rejected(monkeypatch, bad: str) -> None:
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, bad)
    with pytest.raises(ValueError, match=ROUTER_BASE_URL_ENV_VAR):
        Comfy(api_key="comfyui-test")


def test_the_models_namespace_reports_the_router_not_the_v2_deployment(monkeypatch, server) -> None:
    # The distinction this whole binding rests on: jobs and assets resolve
    # under COMFY_BASE_URL, model runs under COMFY_ROUTER_BASE_URL, and they
    # are different hosts by default.
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, "https://router.example")
    with Comfy() as client:
        assert client.models.base_url == "https://router.example"
        assert client._low.base_url == server.base_url
        assert repr(client.models) == "Models(base_url='https://router.example')"


async def test_the_async_namespace_reports_the_router_too(monkeypatch, server) -> None:
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, "https://router.example")
    async with AsyncComfy() as client:
        assert client.models.base_url == "https://router.example"
        assert repr(client.models) == "AsyncModels(base_url='https://router.example')"


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


def test_the_key_reaches_the_router_when_it_is_a_separate_origin(
    monkeypatch, server, second_server
) -> None:
    # The production shape: `COMFY_BASE_URL` and `COMFY_ROUTER_BASE_URL` are
    # different hosts. The client's own credential goes to *both* of its
    # configured targets — otherwise every real model run would be a 401.
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, second_server.base_url)
    with Comfy(api_key="k-router") as client:
        client.models.run(MODEL, ARGS)
    assert second_server.state.last_auth_header == "Bearer k-router"
    assert second_server.state.model_run_count == 1
    # ...and the run went to the router, not to the v2 deployment.
    assert server.state.model_run_count == 0


def test_the_key_is_not_sent_to_a_third_origin(monkeypatch, server, second_server) -> None:
    # Neither configured target: a server-returned absolute follow-up link
    # pointing at `second_server` must still get nothing, and widening the
    # credential rule to cover the router origin must not have widened it to
    # "any absolute URL".
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, "https://router.example")
    with Comfy(api_key="k-not-yours") as client:
        client._low.get_job(f"{second_server.base_url}/api/v2/jobs/whatever")
    assert second_server.state.last_auth_header == ""


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


# --- the key survives the failure ----------------------------------------
#
# The router's replay contract lets a caller who lost a response resend the
# same request under the same `Idempotency-Key` and collect the generation
# they were already billed for. `run` mints that key itself, so unless the
# exception carries it the key dies with the call and a paid-for generation is
# uncollectable — the only callers who could replay were the ones who had
# passed `idempotency_key=` and stored it themselves.


class _RaisingLow:
    """A transport whose every attempt raises ``exc``, recording the key sent.

    A stub HTTP server that is listening cannot produce a connect failure, and
    ``post_model_run`` does not raise ``RouterError`` today — so the two cases
    that never reach the wire are asserted against a transport that can make
    them.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.keys: list[str | None] = []

    def post_model_run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: Any = None,
    ) -> dict[str, Any]:
        self.keys.append(idempotency_key)
        raise self._exc


class _AsyncRaisingLow(_RaisingLow):
    async def post_model_run(  # type: ignore[override]
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: Any = None,
    ) -> dict[str, Any]:
        self.keys.append(idempotency_key)
        raise self._exc


def _models_over(exc: BaseException) -> tuple[Models, _RaisingLow]:
    low = _RaisingLow(exc)
    return Models(cast(Any, low), NO_RETRY), low


def _async_models_over(exc: BaseException) -> tuple[AsyncModels, _RaisingLow]:
    low = _AsyncRaisingLow(exc)
    return AsyncModels(cast(Any, low), NO_RETRY), low


def test_a_deadline_exceeded_504_exposes_the_exact_auto_minted_key(server) -> None:
    # The headline case: Comfy stopped holding the connection at its own bound,
    # the generation may well have completed and been billed, and this
    # attribute is the caller's only route back to it.
    server.state.model_run_error = (504, "deadline_exceeded")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    (sent,) = server.state.model_run_idempotency_keys
    assert sent is not None
    # The exact key, not merely "a key": asserting truthiness would pass on a
    # freshly minted one, which is precisely the value that cannot collect the
    # generation.
    assert excinfo.value.idempotency_key == sent


async def test_an_async_deadline_exceeded_504_exposes_the_key_too(server) -> None:
    server.state.model_run_error = (504, "deadline_exceeded")
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.models.run(MODEL, ARGS)
    (sent,) = server.state.model_run_idempotency_keys
    assert sent is not None
    assert excinfo.value.idempotency_key == sent


def test_a_caller_supplied_key_round_trips_onto_the_exception(server) -> None:
    server.state.model_run_error = (504, "deadline_exceeded")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS, idempotency_key="caller-chosen-03")
    assert excinfo.value.idempotency_key == "caller-chosen-03"
    assert server.state.model_run_idempotency_keys == ["caller-chosen-03"]


async def test_a_caller_supplied_key_round_trips_on_the_async_client(server) -> None:
    server.state.model_run_error = (504, "deadline_exceeded")
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.models.run(MODEL, ARGS, idempotency_key="caller-chosen-04")
    assert excinfo.value.idempotency_key == "caller-chosen-04"


def test_the_replay_idiom_from_the_docstring_collects_the_generation(server) -> None:
    # End to end, against a deployment that replays a claimed key: the 504
    # raises, the caller reads the key off the exception and re-runs under it,
    # and the result of the generation they were already billed for comes back.
    server.state.model_run_error = (504, "deadline_exceeded")
    # The generation completed server side and only the answer was lost, so the
    # stub records the result against the key and serves it back without
    # running the model again. Asserting on the payload alone would not
    # distinguish that from a *second* generation returning an equal payload —
    # which is the double charge this feature exists to avoid — so the run
    # counter below is the assertion that actually holds the contract.
    server.state.model_run_replays_lost_result = True
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
        replayed = client.models.run(MODEL, ARGS, idempotency_key=excinfo.value.idempotency_key)
    assert replayed == server.state.model_run_result
    first, second = server.state.model_run_idempotency_keys
    assert first == second
    # Two requests arrived, one generation happened. The `model_run_error` knob
    # is deliberately left set: the second call succeeds because the key
    # collected the recorded result, not because the failure was turned off.
    assert server.state.model_run_count == 2
    assert server.state.model_run_generations == 1


def test_replaying_without_the_key_would_start_a_second_generation(server) -> None:
    # The negative of the test above, and the reason both the README snippet
    # and the docstring tell a caller to check `idempotency_key` for None: a
    # resend that does not carry the key is a new call, mints a new one, and
    # bills a second generation. Asserted so the guard is not quietly dropped.
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_replays_lost_result = True
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
        server.state.model_run_error = None
        client.models.run(MODEL, ARGS)
    first, second = server.state.model_run_idempotency_keys
    assert first != second
    assert server.state.model_run_generations == 2


def test_a_transport_level_failure_carries_the_key() -> None:
    # No response at all, so nothing to translate — and the case the replay
    # contract names alongside the 504, since a connection dropped mid-run says
    # nothing about whether the generation ran.
    models, low = _models_over(httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError) as excinfo:
        models.run(MODEL, ARGS)
    assert low.keys == [excinfo.value.idempotency_key]  # type: ignore[attr-defined]
    assert excinfo.value.idempotency_key is not None  # type: ignore[attr-defined]


async def test_an_async_transport_level_failure_carries_the_key() -> None:
    models, low = _async_models_over(httpx.ReadTimeout("no answer"))
    with pytest.raises(httpx.ReadTimeout) as excinfo:
        await models.run(MODEL, ARGS)
    assert low.keys == [excinfo.value.idempotency_key]  # type: ignore[attr-defined]


def test_a_client_side_timeout_against_a_live_server_carries_the_key(server) -> None:
    # The read timeout the retry module calls the important member of the
    # unknown-outcome class: the server is most likely still generating.
    server.state.model_run_delay = 0.4
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(httpx.TimeoutException) as excinfo:
            client.models.run(MODEL, ARGS, timeout=0.05)
    (sent,) = server.state.model_run_idempotency_keys
    assert excinfo.value.idempotency_key == sent  # type: ignore[attr-defined]


def test_an_unknown_error_type_lands_on_the_base_router_error_with_the_key() -> None:
    # The reason the stamp lives at the translation boundary rather than in
    # each subclass's constructor: a bucket this SDK version has never heard of
    # falls through to `RouterError` itself, and it has to carry the key too.
    unknown = error_from_response(
        503, {ERROR_TYPE_HEADER: "a_bucket_from_the_future"}, {"detail": "nope"}
    )
    assert type(unknown) is RouterError
    models, low = _models_over(unknown)
    with pytest.raises(RouterError) as excinfo:
        models.run(MODEL, ARGS)
    assert excinfo.value.error_type == "a_bucket_from_the_future"
    assert low.keys == [excinfo.value.idempotency_key]
    assert excinfo.value.idempotency_key is not None


def test_a_typed_router_subclass_carries_the_key_as_well() -> None:
    models, low = _models_over(DeadlineExceeded("we stopped waiting", http_status=504))
    with pytest.raises(DeadlineExceeded) as excinfo:
        models.run(MODEL, ARGS)
    assert low.keys == [excinfo.value.idempotency_key]


def test_the_key_on_the_exception_is_the_one_every_retry_reused(server) -> None:
    # One key across the attempts, and that same key on the exception the
    # exhausted retry finally raises — not the first attempt's, not a fresh one.
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_replays_idempotency_key = True
    policy = RetryPolicy(
        max_elapsed=0.5, initial_backoff=0.01, max_backoff=0.02, retry_possibly_in_flight=True
    )
    with Comfy(retry=policy) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    sent = server.state.model_run_idempotency_keys
    assert len(sent) > 1
    assert set(sent) == {excinfo.value.idempotency_key}


# --- request_id, from the server's own header ----------------------------


def test_a_failed_run_carries_the_servers_request_id(server) -> None:
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_request_id = "req_abc123"
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.request_id == "req_abc123"


async def test_an_async_failed_run_carries_the_servers_request_id(server) -> None:
    server.state.model_run_error = (500, "internal_error")
    server.state.model_run_request_id = "req_def456"
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.models.run(MODEL, ARGS)
    assert excinfo.value.request_id == "req_def456"


def test_request_id_is_none_when_the_response_named_none(server) -> None:
    server.state.model_run_error = (504, "deadline_exceeded")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.request_id is None
    # The key is still there — the two are independent, and the one that
    # enables the replay is minted client-side.
    assert excinfo.value.idempotency_key is not None


def test_request_id_reads_as_none_on_a_failure_with_no_response_at_all() -> None:
    # The documented pair has to be uniform on exactly the failures it is most
    # needed on. `httpx.ConnectError` is not one of this SDK's classes and
    # declares no `request_id`, so without the stamp defaulting it, the
    # attribute access the docs invite would raise AttributeError here.
    models, _ = _models_over(httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError) as excinfo:
        models.run(MODEL, ARGS)
    assert excinfo.value.request_id is None  # type: ignore[attr-defined]
    assert excinfo.value.idempotency_key is not None  # type: ignore[attr-defined]


async def test_an_async_transport_failure_reads_request_id_as_none() -> None:
    models, _ = _async_models_over(httpx.ReadTimeout("no answer"))
    with pytest.raises(httpx.ReadTimeout) as excinfo:
        await models.run(MODEL, ARGS)
    assert excinfo.value.request_id is None  # type: ignore[attr-defined]


def test_a_stamped_error_that_already_has_a_request_id_keeps_it(server) -> None:
    # The default must never overwrite a real id — that would erase the one
    # thing a user quotes in a support request.
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_request_id = "req_keepme"
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.request_id == "req_keepme"


# --- retry_after, the pace the replay is meant to wait for ----------------


def test_a_504_forwards_the_retry_after_the_server_named(server) -> None:
    # The docs tell a caller to replay "after the Retry-After the server
    # named". `deadline_exceeded` is not a throttled bucket, so before this was
    # forwarded for every code the caller was told to wait with nothing to read
    # the wait off.
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "2"
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.retry_after == 2


def test_retry_after_is_none_when_the_server_named_no_pace(server) -> None:
    server.state.model_run_error = (504, "deadline_exceeded")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.retry_after is None


def test_retry_after_reads_as_none_on_a_failure_with_no_response() -> None:
    # The README's table promises all three attributes read rather than raise
    # on anything `models.run` raises, and a transport failure is the case that
    # is neither one of this SDK's classes nor a response.
    models, _ = _models_over(httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError) as excinfo:
        models.run(MODEL, ARGS)
    assert excinfo.value.retry_after is None  # type: ignore[attr-defined]


# --- cancellation, the one BaseException the key rides out on -------------


async def test_cancelling_an_in_flight_run_still_yields_the_key() -> None:
    # `asyncio.wait_for` around a ten-minute run is the ordinary way this
    # happens: the request may already be dispatched and billed, so the key is
    # the caller's route back to it. The cancellation itself must still
    # propagate — it is re-raised bare, not converted.
    models, low = _async_models_over(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError) as excinfo:
        await models.run(MODEL, ARGS)
    assert low.keys == [excinfo.value.idempotency_key]  # type: ignore[attr-defined]
    assert excinfo.value.idempotency_key is not None  # type: ignore[attr-defined]
    assert excinfo.value.request_id is None  # type: ignore[attr-defined]


# --- a success status whose body will not decode --------------------------


def test_an_undecodable_success_body_is_a_stamped_sdk_error(server) -> None:
    # A 200 whose body is not JSON — a proxy interstitial, a truncated
    # response. The generation ran and was billed with the result lost, which
    # is exactly the case the key has to ride out on, so it must not escape as
    # the raw json.JSONDecodeError from outside the translated surface.
    server.state.model_run_undecodable_body = True
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.idempotency_key is not None
    assert excinfo.value.http_status == 200
    assert excinfo.value.code == "invalid_response"
