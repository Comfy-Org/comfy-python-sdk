"""``client.models.run(model, arguments)`` — one call, awaits completion.

Covers the whole contract of the headline model API on both clients: the sync
call blocks and returns the finished result, the async call awaits to the same
shape, the awaitable form is the *async client* rather than a suffixed method
(asserted, not merely absent), the run is addressed to Comfy Router by the
model's two id segments with the model's own native JSON as the body, the
result is that model's native output, the wait is sized for a server that polls
upstream inside the call, an ``Idempotency-Key`` is plumbed onto the wire, and
the result handed back is the provider's own payload rather than a wrapper.

Everything here runs against the stubbed server in ``conftest.py``.
"""

from __future__ import annotations

import inspect
import re

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
)
from comfy_sdk.exceptions import ComfyError, NotFound, Unauthorized
from comfy_sdk.models import AsyncModels, Models

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
    # `/v1/models/{provider}/{model}`, and the body is the partner model's OWN
    # native JSON input, forwarded unchanged — no `{model, arguments}` envelope.
    with Comfy() as client:
        client.models.run(MODEL, ARGS)
    assert server.state.last_model_run_path == "/v1/models/acme/flux-dev"
    assert server.state.last_model_run_provider == "acme"
    assert server.state.last_model_run_model == "flux-dev"
    assert server.state.last_model_run_body == ARGS
    assert "model" not in server.state.last_model_run_body
    assert "arguments" not in server.state.last_model_run_body


async def test_the_async_client_addresses_the_route_the_same_way(server) -> None:
    async with AsyncComfy() as client:
        await client.models.run(MODEL, ARGS)
    assert server.state.last_model_run_path == "/v1/models/acme/flux-dev"
    assert server.state.last_model_run_body == ARGS


def test_the_sans_io_request_builder_agrees_with_the_wire() -> None:
    # The one place the wire shape is decided, asserted directly so a change to
    # it cannot hide behind the stub's own routing.
    path, body, headers = model_run_request(MODEL, ARGS, "k-1")
    assert path == "/v1/models/acme/flux-dev"
    assert body == ARGS
    assert headers == {"Idempotency-Key": "k-1"}


@pytest.mark.parametrize(
    "model, path",
    [
        # `.`, `_` and `-` are all legal *inside* a segment per the spec's
        # `RouterModelSegment` pattern, and none of them is percent-encoded:
        # they are unreserved (or sub-delims) in a path segment, so the URL the
        # caller reads in a log is the id they passed.
        ("fal-ai/flux-pro", "/v1/models/fal-ai/flux-pro"),
        ("acme/sd_xl.turbo", "/v1/models/acme/sd_xl.turbo"),
        ("acme_labs/v1.5", "/v1/models/acme_labs/v1.5"),
        # ...while anything that would change the *structure* of the path is
        # encoded rather than passed through.
        ("acme/a b", "/v1/models/acme/a%20b"),
        ("acme/a?b", "/v1/models/acme/a%3Fb"),
        ("acme/a#b", "/v1/models/acme/a%23b"),
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
    # slash would request `//v1/models/...` — a different path to an origin
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
