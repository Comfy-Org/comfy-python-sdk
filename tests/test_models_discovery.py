"""Model discovery: ``models.list()`` and ``models.schema()``.

Driven against the stub in ``conftest.py`` — ``server.state.catalog_pages`` and
the ``schema_*`` fields set the scenario, and the fixture points the SDK at the
stub through ``COMFY_ROUTER_BASE_URL``, exactly as the ``models.run`` tests do.

What each group here is for:

* the **catalog walk** — every page, followed by ``next_cursor`` while
  ``has_more`` is true, and the single-page form beside it;
* ``limit`` passed through **unchanged** above the maximum (the server clamps);
* the **conditional schema read** — ``If-None-Match`` sent, and a ``304``
  returned as ``unchanged`` rather than raised;
* the **same host, credential and typed errors** as ``models.run``;
* and every one of those on the async client too.
"""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest

from comfy_low.transport import DISCOVERY_TIMEOUT
from comfy_sdk import (
    ROUTER_BASE_URL_ENV_VAR,
    AsyncComfy,
    AsyncModelList,
    CatalogModel,
    Comfy,
    ModelList,
    ModelPage,
    SchemaResult,
)
from comfy_sdk.exceptions import ComfyError
from comfy_sdk.models import AsyncModels, Models
from comfy_sdk.retry import NO_RETRY
from comfy_sdk.router_exceptions import (
    Forbidden,
    InternalError,
    ModelNotFound,
    RouterError,
    ServiceUnavailable,
    Unauthorized,
)

MODEL = "bfl/flux-2-pro"


def _entry(provider: str, model: str) -> dict[str, Any]:
    return {
        "id": f"{provider}/{model}",
        "provider": provider,
        "model": model,
        "billing": {"charges_on_policy_rejection": "no"},
    }


def _three_pages(server) -> None:
    server.state.catalog_pages = {
        None: {
            "data": [_entry("bfl", "flux-2-pro"), _entry("bfl", "flux-2-dev")],
            "has_more": True,
            "next_cursor": "c-2",
            "limit": 2,
        },
        "c-2": {
            "data": [_entry("wan", "wan2.5-i2i-preview")],
            "has_more": True,
            "next_cursor": "c-3",
            "limit": 2,
        },
        # Empty but not last would be legal; the walk ends on `has_more`, not
        # on a short page — so the last page here is deliberately short *and*
        # carries a stale cursor the walk must not follow.
        "c-3": {
            "data": [_entry("acme", "fast-sdxl")],
            "has_more": False,
            "next_cursor": "c-stale",
            "limit": 2,
        },
    }


EXPECTED_IDS = ["bfl/flux-2-pro", "bfl/flux-2-dev", "wan/wan2.5-i2i-preview", "acme/fast-sdxl"]


# --- list(): the walk ------------------------------------------------------


def test_list_walks_every_page_following_next_cursor(server) -> None:
    _three_pages(server)
    with Comfy() as client:
        models = list(client.models.list(limit=2))
    assert [m.id for m in models] == EXPECTED_IDS
    cursors = [q.get("cursor", [None])[0] for q in server.state.catalog_queries]
    assert cursors == [None, "c-2", "c-3"]
    assert all(q["limit"] == ["2"] for q in server.state.catalog_queries)


async def test_async_list_walks_every_page_following_next_cursor(server) -> None:
    _three_pages(server)
    async with AsyncComfy() as client:
        models = [m async for m in client.models.list(limit=2)]
    assert [m.id for m in models] == EXPECTED_IDS
    cursors = [q.get("cursor", [None])[0] for q in server.state.catalog_queries]
    assert cursors == [None, "c-2", "c-3"]


def test_list_entries_expose_id_provider_model_and_billing(server) -> None:
    with Comfy() as client:
        (entry,) = list(client.models.list())
    assert entry == CatalogModel(
        id="bfl/flux-2-pro",
        provider="bfl",
        model="flux-2-pro",
        billing={"charges_on_policy_rejection": "unknown"},
    )


def test_list_is_lazy_and_each_iteration_is_a_fresh_walk(server) -> None:
    with Comfy() as client:
        catalog = client.models.list()
        assert isinstance(catalog, ModelList)
        assert server.state.catalog_queries == []
        assert len(list(catalog)) == 1
        assert len(list(catalog)) == 1
    assert len(server.state.catalog_queries) == 2


def test_list_starts_from_the_cursor_given(server) -> None:
    _three_pages(server)
    with Comfy() as client:
        models = list(client.models.list(cursor="c-2"))
    assert [m.id for m in models] == EXPECTED_IDS[2:]


def test_list_sends_no_query_when_nothing_is_set(server) -> None:
    with Comfy() as client:
        list(client.models.list())
    assert server.state.catalog_queries == [{}]


@pytest.mark.parametrize("limit", [101, 500])
def test_limit_above_the_maximum_is_passed_through_unchanged(server, limit: int) -> None:
    # The route clamps rather than rejects, and reports the size it served —
    # so the SDK sends what it was given and reads the served size back.
    server.state.catalog_pages[None]["limit"] = 100
    with Comfy() as client:
        page = client.models.list(limit=limit).page()
    assert server.state.catalog_queries == [{"limit": [str(limit)]}]
    assert page.limit == 100


def test_a_has_more_page_without_a_cursor_raises_rather_than_looping(server) -> None:
    server.state.catalog_pages[None].update(has_more=True, next_cursor=None)
    with Comfy() as client, pytest.raises(ComfyError) as info:
        list(client.models.list())
    assert info.value.code == "invalid_response"
    assert len(server.state.catalog_queries) == 1


def test_a_cursor_cycle_raises_rather_than_looping(server) -> None:
    _three_pages(server)
    server.state.catalog_pages["c-3"].update(has_more=True, next_cursor="c-2")
    with Comfy() as client, pytest.raises(ComfyError) as info:
        list(client.models.list())
    assert info.value.code == "invalid_response"
    assert len(server.state.catalog_queries) == 3


# --- list().page(): the single-page form -----------------------------------


def test_page_returns_one_page_with_its_paging_facts(server) -> None:
    _three_pages(server)
    with Comfy() as client:
        page = client.models.list(limit=2).page()
    assert isinstance(page, ModelPage)
    assert [m.id for m in page.data] == EXPECTED_IDS[:2]
    assert page.has_more is True
    assert page.next_cursor == "c-2"
    assert page.limit == 2
    assert page.request_id == "req_discovery_01"
    assert len(server.state.catalog_queries) == 1


async def test_async_page_returns_one_page_with_its_paging_facts(server) -> None:
    _three_pages(server)
    async with AsyncComfy() as client:
        catalog = client.models.list(cursor="c-3")
        assert isinstance(catalog, AsyncModelList)
        page = await catalog.page()
    assert [m.id for m in page.data] == EXPECTED_IDS[3:]
    assert page.has_more is False
    assert page.request_id == "req_discovery_01"


# --- schema() ----------------------------------------------------------------


def test_schema_returns_the_document_etag_and_request_id(server) -> None:
    with Comfy() as client:
        result = client.models.schema(MODEL)
    assert result == SchemaResult(
        unchanged=False,
        document=server.state.schema_document,
        etag='"schema-v1"',
        request_id="req_discovery_01",
    )
    assert server.state.schema_paths == ["/v2/models/bfl/flux-2-pro/openapi.json"]
    # No tag given, so the read is unconditional.
    assert server.state.schema_if_none_match == [None]


async def test_async_schema_returns_the_document(server) -> None:
    async with AsyncComfy() as client:
        result = await client.models.schema(MODEL)
    assert result.unchanged is False
    assert result.document == server.state.schema_document
    assert result.etag == '"schema-v1"'


def test_schema_sends_if_none_match_and_a_304_is_unchanged_not_an_error(server) -> None:
    with Comfy() as client:
        result = client.models.schema(MODEL, etag='"schema-v1"')
    assert server.state.schema_if_none_match == ['"schema-v1"']
    assert result == SchemaResult(
        unchanged=True, document=None, etag='"schema-v1"', request_id="req_discovery_01"
    )


async def test_async_schema_304_is_unchanged(server) -> None:
    async with AsyncComfy() as client:
        result = await client.models.schema(MODEL, etag='"schema-v1"')
    assert result.unchanged is True
    assert result.document is None
    assert result.etag == '"schema-v1"'
    assert result.request_id == "req_discovery_01"


def test_a_stale_etag_gets_the_new_document(server) -> None:
    server.state.schema_etag = '"schema-v2"'
    with Comfy() as client:
        result = client.models.schema(MODEL, etag='"schema-v1"')
    assert result.unchanged is False
    assert result.document == server.state.schema_document
    assert result.etag == '"schema-v2"'


def test_schema_percent_encodes_each_segment(server) -> None:
    server.state.schema_models = {"acme/fast sdxl"}
    with Comfy() as client:
        client.models.schema("acme/fast sdxl")
    assert server.state.schema_paths == ["/v2/models/acme/fast%20sdxl/openapi.json"]


@pytest.mark.parametrize("bad", ["bfl", "bfl/flux-2-pro/fp8", "bfl/..", "a//b"])
def test_schema_rejects_a_malformed_id_before_any_request(server, bad: str) -> None:
    with Comfy() as client, pytest.raises(ValueError):
        client.models.schema(bad)
    assert server.state.schema_paths == []


def test_schema_404_for_an_unknown_model_raises_model_not_found(server) -> None:
    with Comfy(retry=NO_RETRY) as client, pytest.raises(ModelNotFound) as info:
        client.models.schema("bfl/no-such-model")
    assert info.value.http_status == 404
    assert info.value.error_type == "model_not_found"


async def test_async_schema_404_for_an_unknown_model_raises_model_not_found(server) -> None:
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ModelNotFound):
            await client.models.schema("bfl/no-such-model")


# --- same host, credential, timeout and typed errors as run() -------------


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (401, "unauthorized", Unauthorized),
        (403, "forbidden", Forbidden),
        (500, "internal_error", InternalError),
        (503, "service_unavailable", ServiceUnavailable),
    ],
)
def test_discovery_failures_raise_the_typed_router_exception(
    server, status: int, code: str, cls: type[RouterError]
) -> None:
    server.state.catalog_error = (status, code)
    server.state.schema_error = (status, code)
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(cls) as listed:
            list(client.models.list())
        with pytest.raises(cls) as schema:
            client.models.schema(MODEL)
    assert listed.value.http_status == status
    assert schema.value.http_status == status


async def test_async_list_failure_raises_the_typed_router_exception(server) -> None:
    server.state.catalog_error = (403, "forbidden")
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(Forbidden):
            await client.models.list().page()


def test_discovery_goes_to_the_router_with_the_clients_credential(
    monkeypatch, server, second_server
) -> None:
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, second_server.base_url)
    second_server.state.require_auth = True
    with Comfy(api_key="k-discover") as client:
        list(client.models.list())
        assert second_server.state.last_auth_header == "Bearer k-discover"
        client.models.schema(MODEL)
        assert second_server.state.last_auth_header == "Bearer k-discover"
    assert len(second_server.state.catalog_queries) == 1
    assert len(second_server.state.schema_paths) == 1
    # ...and nothing went to the v2 deployment.
    assert server.state.catalog_queries == []
    assert server.state.schema_paths == []


def test_discovery_retries_a_transient_failure_under_the_client_policy(server, monkeypatch) -> None:
    # A 429 naming Retry-After is retried by the default policy, exactly as on
    # every other Router route this namespace owns.
    import comfy_sdk.model_catalog as catalog

    monkeypatch.setattr(catalog.time, "sleep", lambda _s: None)
    server.state.catalog_fail_times = 1
    with Comfy() as client:
        assert len(list(client.models.list())) == 1
    assert len(server.state.catalog_queries) == 2


@pytest.mark.parametrize("cls", [Models, AsyncModels])
@pytest.mark.parametrize("method", ["list", "schema"])
def test_discovery_defaults_to_a_30_second_timeout(cls: type, method: str) -> None:
    default = inspect.signature(getattr(cls, method)).parameters["timeout"].default
    assert default is DISCOVERY_TIMEOUT
    assert isinstance(default, httpx.Timeout)
    assert (default.connect, default.read, default.write, default.pool) == (30.0,) * 4


def test_a_timeout_reaches_the_request(server, monkeypatch) -> None:
    seen: list[Any] = []
    with Comfy() as client:
        real = client._low.raw_request

        def spy(*args: Any, **kwargs: Any) -> httpx.Response:
            seen.append(kwargs.get("timeout"))
            return real(*args, **kwargs)

        monkeypatch.setattr(client._low, "raw_request", spy)
        list(client.models.list())
        client.models.schema(MODEL, timeout=5.0)
    assert seen == [DISCOVERY_TIMEOUT, 5.0]


def test_a_304_to_an_unconditional_read_is_not_reported_as_unchanged(server) -> None:
    # Only a request that sent If-None-Match can be told "your copy is
    # current"; a caller with no tag has no copy, so a stray 304 must raise.
    with Comfy(retry=NO_RETRY) as client:
        real = client._low.raw_request

        def as_304(*args: Any, **kwargs: Any) -> httpx.Response:
            real(*args, **kwargs)
            return httpx.Response(304, headers={"ETag": '"x"'})

        client._low.raw_request = as_304  # type: ignore[method-assign]
        with pytest.raises(ComfyError):
            client.models.schema(MODEL)
