"""``client.models`` — the model namespace on the existing client.

What makes it a namespace rather than a second client object: it is reachable
from a client you already constructed, and it reads that client's *live*
configuration — credentials, base URL, transport, timeout — instead of a copy
taken at construction. Both properties are asserted here, including a config
change made on the client after construction being visible through ``models``.
"""

from __future__ import annotations

import httpx

import comfy_sdk
from comfy_low.transport import ComfyLow
from comfy_sdk import AsyncComfy, Comfy
from comfy_sdk.models import AsyncModels, Models


def test_models_is_reachable_from_a_constructed_client(server) -> None:
    with Comfy() as client:
        assert isinstance(client.models, Models)


async def test_async_models_is_reachable_from_a_constructed_client(server) -> None:
    async with AsyncComfy() as client:
        assert isinstance(client.models, AsyncModels)


def test_models_holds_the_host_clients_transport(server) -> None:
    # Identity, not equality: the same transport object means the same
    # connection pool, credential, base URL and timeout — nothing to keep in
    # sync, and nothing a second client would have forked.
    with Comfy() as client:
        assert client.models._low is client._low


async def test_async_models_holds_the_host_clients_transport(server) -> None:
    async with AsyncComfy() as client:
        assert client.models._low is client._low


def test_models_reports_the_routers_base_url(server) -> None:
    # The namespace's own target, read live off the shared transport — Router,
    # not the client's `/api/v2` deployment. The `server` fixture points both
    # at the one stub, so `client._low.router_base_url` is what is asserted
    # rather than the coincidence of the two being equal here; the separate-host
    # case lives in tests/test_models_run.py.
    with Comfy() as client:
        assert client.models.base_url == client._low.router_base_url == server.base_url


async def test_async_models_reports_the_routers_base_url(server) -> None:
    async with AsyncComfy() as client:
        assert client.models.base_url == client._low.router_base_url == server.base_url


def test_a_timeout_change_on_the_client_is_visible_through_models(server) -> None:
    with Comfy(timeout=30.0) as client:
        assert client.models.timeout.read == 30.0
        # Changed on the client *after* construction: models must follow it,
        # which a copied-config namespace would not.
        client._low._client.timeout = httpx.Timeout(1.25)
        assert client.models.timeout.read == 1.25


async def test_a_timeout_change_on_the_async_client_is_visible_through_models(server) -> None:
    async with AsyncComfy(timeout=30.0) as client:
        assert client.models.timeout.read == 30.0
        client._low._client.timeout = httpx.Timeout(1.25)
        assert client.models.timeout.read == 1.25


# --- the pool the namespace shares --------------------------------------
#
# `models.run` holds its pooled connection for the whole generation, so the
# pool's ceiling is what caps a fan-out of concurrent runs. These reach into
# httpx/httpcore internals (`_transport._pool._max_connections`) because httpx
# exposes no public getter for the limits a client was built with; the
# alternative is asserting nothing about whether `limits=` arrived at all.


def test_limits_sizes_the_pool_the_client_builds(server) -> None:
    with Comfy(limits=httpx.Limits(max_connections=250)) as client:
        assert client._low._client._transport._pool._max_connections == 250


async def test_limits_sizes_the_pool_the_async_client_builds(server) -> None:
    async with AsyncComfy(limits=httpx.Limits(max_connections=250)) as client:
        assert client._low._client._transport._pool._max_connections == 250


def test_no_limits_leaves_httpxs_own_default_pool(server) -> None:
    # `limits=None` is dropped rather than forwarded — httpx types the
    # parameter as a `Limits` and does not accept `None` — so the default path
    # has to land on httpx's own DEFAULT_LIMITS, 100 connections.
    with Comfy() as client:
        assert client._low._client._transport._pool._max_connections == 100


async def test_no_limits_leaves_the_async_client_httpxs_default_pool(server) -> None:
    async with AsyncComfy() as client:
        assert client._low._client._transport._pool._max_connections == 100


def test_an_injected_client_keeps_its_own_pool(server) -> None:
    # Injecting `client=` hands pool ownership to the caller, so `limits` is
    # ignored there exactly as `timeout` already is: the pool stays the
    # injected client's default 100, not the 5 asked for here.
    with httpx.Client() as injected:
        low = ComfyLow(
            server.base_url,
            "ck_test",
            client=injected,
            limits=httpx.Limits(max_connections=5),
        )
        assert low._client is injected
        assert low._client._transport._pool._max_connections == 100


def test_models_sends_the_host_clients_credentials(server) -> None:
    server.state.require_auth = True
    with Comfy(api_key="k-first") as client:
        # The transport a model request would go out on is the client's own,
        # so it carries the client's bearer token to the client's base URL.
        client.models._low.get_job("job_01")
        assert server.state.last_auth_header == "Bearer k-first"

        # And a credential rotated on the client is picked up through models.
        client._low._p.api_key = "k-rotated"
        client.models._low.get_job("job_01")
        assert server.state.last_auth_header == "Bearer k-rotated"


async def test_async_models_sends_the_host_clients_credentials(server) -> None:
    server.state.require_auth = True
    async with AsyncComfy(api_key="k-async") as client:
        await client.models._low.get_job("job_01")
        assert server.state.last_auth_header == "Bearer k-async"


def test_two_clients_get_independent_namespaces(server) -> None:
    with Comfy() as one, Comfy() as two:
        assert one.models is not two.models
        assert one.models._low is not two.models._low


def test_models_repr_names_the_routers_base_url(server) -> None:
    with Comfy() as client:
        assert repr(client.models) == f"Models(base_url={server.base_url!r})"


def test_the_namespace_adds_no_new_top_level_import_path() -> None:
    # ``from comfy_sdk import Comfy`` stays the only path a caller needs:
    # the namespace is reached as ``client.models``, so neither class is
    # exported at the top level.
    for name in ("Models", "AsyncModels"):
        assert name not in comfy_sdk.__all__
        assert not hasattr(comfy_sdk, name)
