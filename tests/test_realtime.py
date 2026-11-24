"""``client.realtime.connect`` against loopback servers.

Two servers, both on ``127.0.0.1`` in a background thread: a real
``websockets`` server that accepts the upgrade and records what the client
sent, and a raw ``asyncio`` HTTP server that answers the upgrade with a scripted
status and Router error body, which is how Router refuses a session.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import importlib
import json
import logging
import os
import threading
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers

from comfy_sdk import AsyncComfy, Comfy, ComfyError, MissingApiKey
from comfy_sdk.client import BASE_URL_ENV_VAR, ROUTER_BASE_URL_ENV_VAR
from comfy_sdk.realtime import (
    _REALTIME_SERVING_PROVIDERS,
    REALTIME_SUBPROTOCOL,
    AsyncRealtimeSession,
    RealtimeSession,
    _plan,
    _realtime_path,
)
from comfy_sdk.router_exceptions import (
    ConcurrencyLimitExceeded,
    InsufficientCredits,
    RouterError,
    ServiceUnavailable,
)

_KEY = "comfyui-realtime-test-key"
_REQUEST_ID = "req-realtime-1"


class _Loop:
    """An event loop in a daemon thread, for servers the sync client can reach."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        self.loop.close()


@dataclass
class _Seen:
    path: str | None = None
    headers: Headers | None = None
    paths: list[str] = field(default_factory=list)


@pytest.fixture
def loop() -> Iterator[_Loop]:
    lp = _Loop()
    yield lp
    lp.stop()


@pytest.fixture
def ws_server(loop: _Loop, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Seen]:
    seen = _Seen()

    async def handler(ws: ServerConnection) -> None:
        seen.path = ws.request.path
        seen.paths.append(ws.request.path)
        seen.headers = ws.request.headers
        async for message in ws:
            await ws.send(message)

    def stamp(ws: ServerConnection, request: Any, response: Any) -> Any:
        response.headers["X-Comfy-Request-Id"] = _REQUEST_ID
        return response

    async def start() -> Any:
        return await serve(
            handler,
            "127.0.0.1",
            0,
            subprotocols=[REALTIME_SUBPROTOCOL],  # type: ignore[list-item]
            process_response=stamp,
        )

    server = loop.run(start())
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, f"http://127.0.0.1:{port}")
    yield seen
    server.close()
    loop.run(server.wait_closed())


@dataclass
class _Script:
    status: int = 402
    reason: str = "Payment Required"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@pytest.fixture
def refusing_server(loop: _Loop, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Script]:
    script = _Script()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        head = [f"HTTP/1.1 {script.status} {script.reason}"]
        head += [f"{k}: {v}" for k, v in script.headers.items()]
        head += [f"Content-Length: {len(script.body)}", "Connection: close"]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + script.body)
        await writer.drain()
        writer.close()

    async def start() -> Any:
        return await asyncio.start_server(handle, "127.0.0.1", 0)

    server = loop.run(start())
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, f"http://127.0.0.1:{port}")
    yield script
    server.close()
    loop.run(server.wait_closed())


def _offer(seen: _Seen) -> list[str]:
    assert seen.headers is not None
    raw = ", ".join(seen.headers.get_all("Sec-WebSocket-Protocol"))
    return [p.strip() for p in raw.split(",") if p.strip()]


# -- model id -> path ---------------------------------------------------------


def test_serving_table() -> None:
    assert _REALTIME_SERVING_PROVIDERS == {"google/genie-4": "google"}
    assert _realtime_path("google/genie-4") == ("google", "/v2/realtime/google/google/genie-4")
    assert _realtime_path("xai/grok-voice/realtime") == (
        "fal",
        "/v2/realtime/fal/xai/grok-voice/realtime",
    )
    assert _realtime_path("xai/grok-voice/realtime", provider="google") == (
        "google",
        "/v2/realtime/google/xai/grok-voice/realtime",
    )


@pytest.mark.parametrize(
    ("model", "pointer"),
    [
        ("fal-ai/lingbot-world-2", "POST /v2/realtime/wma/fal"),
        ("fal-ai/abot-world-0", "POST /v2/realtime/wma/fal"),
        ("fish/live-3", "POST /v2/realtime/fish/sessions"),
        ("reactor/anything", "not served"),
    ],
)
def test_models_on_other_surfaces_refuse_before_any_socket(
    ws_server: _Seen, model: str, pointer: str
) -> None:
    with Comfy(api_key=_KEY) as client:
        with pytest.raises(ComfyError) as info:
            client.realtime.connect(model)
    assert info.value.code == "realtime_unsupported_model"
    assert pointer in str(info.value)
    assert ws_server.paths == []


@pytest.mark.parametrize(
    "model",
    ["genie-4", "google//genie-4", "google/../x", "google/./x", "Google/genie-4", "a/b c", ""],
)
def test_invalid_ids_raise(model: str) -> None:
    with Comfy(api_key=_KEY) as client, pytest.raises(ValueError):
        client.realtime.connect(model)


def test_invalid_provider_raises() -> None:
    with Comfy(api_key=_KEY) as client, pytest.raises(ValueError):
        client.realtime.connect("google/genie-4", provider="fal/x")


# -- sync client --------------------------------------------------------------


def test_key_mode_sync(ws_server: _Seen) -> None:
    with Comfy(api_key=_KEY) as client, client.realtime.connect("google/genie-4") as session:
        assert isinstance(session, RealtimeSession)
        session.socket.send("ping")
        assert session.socket.recv() == "ping"
        assert session.provider == "google"
        assert session.wire_format == "json"
        assert session.subprotocol == REALTIME_SUBPROTOCOL
        assert session.request_id == _REQUEST_ID
        assert session.url.endswith("/v2/realtime/google/google/genie-4")
        assert session.url.startswith("ws://")
    assert ws_server.path == "/v2/realtime/google/google/genie-4"
    headers = ws_server.headers
    assert headers is not None
    assert headers["x-api-key"] == _KEY
    assert "authorization" not in headers
    assert headers["user-agent"].startswith("comfy-sdk-python/")
    assert headers["user-agent"] == client._low._p._user_agent
    assert _offer(ws_server) == [REALTIME_SUBPROTOCOL]


def test_id_token_mode_sync(ws_server: _Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    # A deployment other than Comfy Cloud, so the client builds with no key.
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://127.0.0.1:1")
    with Comfy() as client:
        session = client.realtime.connect("xai/grok-voice/realtime", id_token="tok.en-1")
        try:
            assert session.subprotocol == REALTIME_SUBPROTOCOL
            assert session.provider == "fal"
            assert session.wire_format == "msgpack"
        finally:
            session.close()
    assert ws_server.path == "/v2/realtime/fal/xai/grok-voice/realtime"
    headers = ws_server.headers
    assert headers is not None
    assert "x-api-key" not in headers
    assert "authorization" not in headers
    assert _offer(ws_server) == [REALTIME_SUBPROTOCOL, "comfy-bearer.tok.en-1"]
    assert "tok.en-1" not in session.url


def test_id_token_wins_over_the_key(ws_server: _Seen) -> None:
    with Comfy(api_key=_KEY) as client:
        client.realtime.connect("google/genie-4", id_token="t", protocols=["extra"]).close()
    assert ws_server.headers is not None
    assert "x-api-key" not in ws_server.headers
    assert _offer(ws_server) == [REALTIME_SUBPROTOCOL, "comfy-bearer.t", "extra"]


def test_id_token_cannot_smuggle_entries(ws_server: _Seen) -> None:
    with Comfy(api_key=_KEY) as client, pytest.raises(ValueError):
        client.realtime.connect("google/genie-4", id_token="a, b")
    assert ws_server.paths == []


def test_a_bare_string_of_protocols_is_refused() -> None:
    with Comfy(api_key=_KEY) as client, pytest.raises(TypeError):
        client.realtime.connect("google/genie-4", protocols="extra")


def test_no_credential_raises_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://127.0.0.1:1")
    with Comfy() as client, pytest.raises(MissingApiKey):
        client.realtime.connect("google/genie-4")


def test_402_refusal_is_typed(refusing_server: _Script) -> None:
    refusing_server.status, refusing_server.reason = 402, "Payment Required"
    refusing_server.headers = {
        "Content-Type": "application/json",
        "X-Comfy-Error-Type": "insufficient_credits",
        "X-Comfy-Request-Id": _REQUEST_ID,
    }
    refusing_server.body = json.dumps(
        {
            "detail": "out of credits",
            "error_type": "insufficient_credits",
            "code": "router.realtime.insufficient_credits",
        }
    ).encode()
    with Comfy(api_key=_KEY) as client, pytest.raises(InsufficientCredits) as info:
        client.realtime.connect("google/genie-4")
    assert info.value.http_status == 402
    assert info.value.refusal_code == "router.realtime.insufficient_credits"
    assert info.value.request_id == _REQUEST_ID
    assert info.value.detail == "out of credits"


def test_429_refusal_carries_retry_after(refusing_server: _Script) -> None:
    refusing_server.status, refusing_server.reason = 429, "Too Many Requests"
    refusing_server.headers = {"Content-Type": "application/json", "Retry-After": "7"}
    refusing_server.body = json.dumps(
        {
            "detail": "a session is already live",
            "error_type": "concurrency_limit_exceeded",
            "code": "router.realtime.session_already_live",
        }
    ).encode()
    with Comfy(api_key=_KEY) as client, pytest.raises(ConcurrencyLimitExceeded) as info:
        client.realtime.connect("xai/grok-voice/realtime")
    assert info.value.retry_after == 7
    assert info.value.refusal_code == "router.realtime.session_already_live"


def test_503_from_router_is_service_unavailable(refusing_server: _Script) -> None:
    refusing_server.status, refusing_server.reason = 503, "Service Unavailable"
    refusing_server.headers = {"X-Comfy-Error-Type": "service_unavailable"}
    with Comfy(api_key=_KEY) as client, pytest.raises(ServiceUnavailable) as info:
        client.realtime.connect("google/genie-4")
    assert info.value.http_status == 503
    assert info.value.refusal_code is None


def test_bare_503_stays_an_untyped_router_error(refusing_server: _Script) -> None:
    # No body and no bucket header: an intermediary's 503, which the shared
    # status fallback deliberately does not read as Router's own
    # `service_unavailable` (see `_ERROR_TYPE_BY_STATUS`).
    refusing_server.status, refusing_server.reason = 503, "Service Unavailable"
    with Comfy(api_key=_KEY) as client, pytest.raises(RouterError) as info:
        client.realtime.connect("google/genie-4")
    assert type(info.value) is RouterError
    assert info.value.http_status == 503


def test_unreachable_host_is_upgrade_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, "http://127.0.0.1:1")
    with Comfy(api_key=_KEY) as client, pytest.raises(ComfyError) as info:
        client.realtime.connect("google/genie-4", open_timeout=2)
    assert info.value.code == "realtime_upgrade_failed"


def test_missing_websockets_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "websockets" or name.startswith("websockets."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with Comfy(api_key=_KEY) as client, pytest.raises(ImportError) as info:
        client.realtime.connect("google/genie-4")
    assert "pip install 'comfy-sdk[realtime]'" in str(info.value)


# -- async client -------------------------------------------------------------


async def test_key_mode_async(ws_server: _Seen) -> None:
    async with AsyncComfy(api_key=_KEY) as client:
        async with await client.realtime.connect("xai/grok-voice/realtime") as session:
            assert isinstance(session, AsyncRealtimeSession)
            await session.socket.send(b"\x80")
            assert await session.socket.recv() == b"\x80"
            assert session.wire_format == "msgpack"
            assert session.subprotocol == REALTIME_SUBPROTOCOL
            assert session.request_id == _REQUEST_ID
    assert ws_server.path == "/v2/realtime/fal/xai/grok-voice/realtime"
    assert ws_server.headers is not None
    assert ws_server.headers["x-api-key"] == _KEY
    assert ws_server.headers["user-agent"].startswith("comfy-sdk-python/")
    assert _offer(ws_server) == [REALTIME_SUBPROTOCOL]


async def test_id_token_mode_async(ws_server: _Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://127.0.0.1:1")
    async with AsyncComfy() as client:
        session = await client.realtime.connect("google/genie-4", id_token="tok")
        assert session.subprotocol == REALTIME_SUBPROTOCOL
        await session.aclose()
    assert ws_server.headers is not None
    assert "x-api-key" not in ws_server.headers
    assert _offer(ws_server) == [REALTIME_SUBPROTOCOL, "comfy-bearer.tok"]


async def test_refusal_async(refusing_server: _Script) -> None:
    refusing_server.status, refusing_server.reason = 429, "Too Many Requests"
    refusing_server.headers = {
        "Content-Type": "application/json",
        "Retry-After": "7",
        "X-Comfy-Error-Type": "concurrency_limit_exceeded",
    }
    refusing_server.body = b'{"detail": "busy", "code": "router.realtime.concurrency_limit"}'
    async with AsyncComfy(api_key=_KEY) as client:
        with pytest.raises(ConcurrencyLimitExceeded) as info:
            await client.realtime.connect("google/genie-4")
    assert info.value.retry_after == 7
    assert info.value.refusal_code == "router.realtime.concurrency_limit"


async def test_unsupported_model_async() -> None:
    async with AsyncComfy(api_key=_KEY) as client:
        with pytest.raises(ComfyError) as info:
            await client.realtime.connect("fish/live-3")
    assert info.value.code == "realtime_unsupported_model"


# -- credential containment ---------------------------------------------------

_HOP = "/v2/realtime/fal/redir/hop"
_LANDED = "/v2/realtime/fal/redir/landed"
#: Whether this websockets release follows redirects in its sync client (17+).
_SYNC_FOLLOWS = hasattr(importlib.import_module("websockets.sync.client"), "reconnect")
#: Whether this websockets release follows redirects in its asyncio client (14+).
_ASYNC_FOLLOWS = hasattr(
    importlib.import_module("websockets.asyncio.client").connect, "process_redirect"
)


@dataclass
class _Pair:
    """A Router origin that redirects ``_HOP``, and a second origin beside it."""

    router: _Seen
    other: _Seen
    other_port: int
    location: str = _LANDED


@pytest.fixture
def redirect_pair(loop: _Loop, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Pair]:
    def recorder(seen: _Seen) -> Any:
        async def handler(ws: ServerConnection) -> None:
            seen.path = ws.request.path
            seen.paths.append(ws.request.path)
            seen.headers = ws.request.headers
            await ws.wait_closed()

        return handler

    router, other = _Seen(), _Seen()
    pair: _Pair

    def hop(ws: ServerConnection, request: Any) -> Any:
        if request.path != _HOP:
            return None
        response = ws.respond(HTTPStatus.FOUND, "")
        response.headers["Location"] = pair.location
        return response

    async def start(handler: Any, process_request: Any = None) -> Any:
        return await serve(
            handler,
            "127.0.0.1",
            0,
            subprotocols=[REALTIME_SUBPROTOCOL],  # type: ignore[list-item]
            process_request=process_request,
        )

    router_server = loop.run(start(recorder(router), hop))
    other_server = loop.run(start(recorder(other)))
    port = router_server.sockets[0].getsockname()[1]
    other_port = other_server.sockets[0].getsockname()[1]
    pair = _Pair(router=router, other=other, other_port=other_port)
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, f"http://127.0.0.1:{port}")
    yield pair
    for server in (router_server, other_server):
        server.close()
        loop.run(server.wait_closed())


_AUTH_MODES = [pytest.param({}, id="api-key"), pytest.param({"id_token": "tok"}, id="id-token")]


@pytest.mark.parametrize("auth", _AUTH_MODES)
def test_cross_origin_redirect_is_refused_sync(redirect_pair: _Pair, auth: dict[str, str]) -> None:
    redirect_pair.location = f"ws://127.0.0.1:{redirect_pair.other_port}{_LANDED}"
    with Comfy(api_key=_KEY) as client, pytest.raises(RouterError) as info:
        client.realtime.connect("redir/hop", **auth)
    assert info.value.http_status == 302
    assert redirect_pair.other.paths == []


@pytest.mark.parametrize("auth", _AUTH_MODES)
async def test_cross_origin_redirect_is_refused_async(
    redirect_pair: _Pair, auth: dict[str, str]
) -> None:
    redirect_pair.location = f"ws://127.0.0.1:{redirect_pair.other_port}{_LANDED}"
    async with AsyncComfy(api_key=_KEY) as client:
        with pytest.raises(RouterError) as info:
            await client.realtime.connect("redir/hop", **auth)
    assert info.value.http_status == 302
    assert redirect_pair.other.paths == []


@pytest.mark.parametrize("auth", _AUTH_MODES)
def test_same_origin_redirect_sync(redirect_pair: _Pair, auth: dict[str, str]) -> None:
    with Comfy(api_key=_KEY) as client:
        if not _SYNC_FOLLOWS:
            # Before websockets 17 the sync client follows no redirect at all.
            with pytest.raises(RouterError):
                client.realtime.connect("redir/hop", **auth)
            return
        client.realtime.connect("redir/hop", **auth).close()
    assert redirect_pair.router.path == _LANDED
    assert redirect_pair.router.headers is not None
    if auth:
        assert _offer(redirect_pair.router) == [REALTIME_SUBPROTOCOL, "comfy-bearer.tok"]
    else:
        assert redirect_pair.router.headers["x-api-key"] == _KEY


@pytest.mark.parametrize("auth", _AUTH_MODES)
async def test_same_origin_redirect_async(redirect_pair: _Pair, auth: dict[str, str]) -> None:
    async with AsyncComfy(api_key=_KEY) as client:
        if not _ASYNC_FOLLOWS:
            # websockets 13's asyncio client follows no redirect at all.
            with pytest.raises(RouterError) as info:
                await client.realtime.connect("redir/hop", **auth)
            assert info.value.http_status == 302
            return
        await (await client.realtime.connect("redir/hop", **auth)).aclose()
    assert redirect_pair.router.path == _LANDED
    assert redirect_pair.router.headers is not None
    if auth:
        assert _offer(redirect_pair.router) == [REALTIME_SUBPROTOCOL, "comfy-bearer.tok"]
    else:
        assert redirect_pair.router.headers["x-api-key"] == _KEY


@pytest.mark.parametrize("auth", _AUTH_MODES)
async def test_same_origin_redirect_drops_userinfo_async(
    redirect_pair: _Pair, auth: dict[str, str]
) -> None:
    if not _ASYNC_FOLLOWS:
        pytest.skip("this websockets release follows no asyncio redirect")
    port = urlsplit(os.environ[ROUTER_BASE_URL_ENV_VAR]).port
    redirect_pair.location = f"ws://user:pw@127.0.0.1:{port}{_LANDED}"
    async with AsyncComfy(api_key=_KEY) as client:
        await (await client.realtime.connect("redir/hop", **auth)).aclose()
    assert redirect_pair.router.path == _LANDED
    assert redirect_pair.router.headers is not None
    assert "authorization" not in redirect_pair.router.headers


@pytest.mark.parametrize("auth", _AUTH_MODES)
def test_same_origin_redirect_drops_userinfo_sync(
    redirect_pair: _Pair, auth: dict[str, str]
) -> None:
    if not _SYNC_FOLLOWS:
        pytest.skip("this websockets release follows no sync redirect")
    port = urlsplit(os.environ[ROUTER_BASE_URL_ENV_VAR]).port
    redirect_pair.location = f"ws://user:pw@127.0.0.1:{port}{_LANDED}"
    with Comfy(api_key=_KEY) as client:
        client.realtime.connect("redir/hop", **auth).close()
    assert redirect_pair.router.path == _LANDED
    assert redirect_pair.router.headers is not None
    assert "authorization" not in redirect_pair.router.headers


def test_plan_repr_carries_no_credential() -> None:
    with Comfy(api_key=_KEY) as client:
        keyed = _plan(client._low, "google/genie-4", None, None, ())
        tokened = _plan(client._low, "google/genie-4", "secret-token", None, ())
    assert _KEY not in repr(keyed)
    assert "secret-token" not in repr(tokened)


def test_userinfo_never_reaches_the_upgrade(
    ws_server: _Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    base = os.environ[ROUTER_BASE_URL_ENV_VAR].replace("http://", "http://user:pass@")
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, base)
    with Comfy(api_key=_KEY) as client:
        session = client.realtime.connect("google/genie-4", id_token="tok")
        session.close()
    assert ws_server.headers is not None
    assert "authorization" not in ws_server.headers
    assert "pass" not in session.url


@pytest.mark.parametrize("entry", ["a, b", "a b", "", "comfy-bearer.other", b"x"])
def test_protocols_entries_are_validated(ws_server: _Seen, entry: Any) -> None:
    with Comfy(api_key=_KEY) as client, pytest.raises(ValueError):
        client.realtime.connect("google/genie-4", protocols=[entry])
    assert ws_server.paths == []


def test_deeply_nested_refusal_body_stays_typed(refusing_server: _Script) -> None:
    refusing_server.status, refusing_server.reason = 402, "Payment Required"
    refusing_server.body = b"[" * 100_000
    with Comfy(api_key=_KEY) as client, pytest.raises(RouterError) as info:
        client.realtime.connect("google/genie-4")
    assert info.value.http_status == 402


def test_repeated_request_id_header_is_tolerated(
    loop: _Loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.wait_closed()

    def stamp(ws: ServerConnection, request: Any, response: Any) -> Any:
        response.headers["X-Comfy-Request-Id"] = "req-first"
        response.headers["X-Comfy-Request-Id"] = "req-second"
        return response

    async def start() -> Any:
        return await serve(
            handler,
            "127.0.0.1",
            0,
            subprotocols=[REALTIME_SUBPROTOCOL],  # type: ignore[list-item]
            process_response=stamp,
        )

    server = loop.run(start())
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, f"http://127.0.0.1:{port}")
    try:
        with Comfy(api_key=_KEY) as client, client.realtime.connect("google/genie-4") as session:
            assert session.request_id == "req-first"
    finally:
        server.close()
        loop.run(server.wait_closed())


def test_max_size_is_passed_through(ws_server: _Seen) -> None:
    from websockets.exceptions import ConnectionClosedError

    with Comfy(api_key=_KEY) as client:
        session = client.realtime.connect("google/genie-4", max_size=8)
        session.socket.send("x" * 64)
        with pytest.raises(ConnectionClosedError) as info:
            session.socket.recv()
    assert info.value.sent is not None and info.value.sent.code == 1009


# -- catalog ids, credential-safe logs and errors -----------------------------


def test_catalog_id_resolves_to_its_endpoint_path(ws_server: _Seen) -> None:
    # The catalog lists `xai/grok-voice`; the route serves it under `/realtime`.
    assert _realtime_path("xai/grok-voice") == ("fal", "/v2/realtime/fal/xai/grok-voice/realtime")
    with Comfy(api_key=_KEY) as client, client.realtime.connect("xai/grok-voice") as session:
        assert session.model == "xai/grok-voice"
    assert ws_server.path == "/v2/realtime/fal/xai/grok-voice/realtime"


_SECRET_TOKEN = "tok.secret-bearer-1"
_SECRET_MODES = [
    pytest.param({}, _KEY, id="api-key"),
    pytest.param({"id_token": _SECRET_TOKEN}, _SECRET_TOKEN, id="id-token"),
]


@pytest.mark.parametrize(("auth", "secret"), _SECRET_MODES)
def test_debug_logging_masks_the_credential_sync(
    ws_server: _Seen, caplog: pytest.LogCaptureFixture, auth: dict[str, str], secret: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger="websockets.client"):
        with Comfy(api_key=_KEY) as client, client.realtime.connect("google/genie-4", **auth):
            pass
    assert "> GET /v2/realtime/google/google/genie-4" in caplog.text
    assert secret not in caplog.text
    assert "***" in caplog.text


@pytest.mark.parametrize(("auth", "secret"), _SECRET_MODES)
async def test_debug_logging_masks_the_credential_async(
    ws_server: _Seen, caplog: pytest.LogCaptureFixture, auth: dict[str, str], secret: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger="websockets.client"):
        async with AsyncComfy(api_key=_KEY) as client:
            async with await client.realtime.connect("google/genie-4", **auth):
                pass
    assert "> GET /v2/realtime/google/google/genie-4" in caplog.text
    assert secret not in caplog.text
    assert "***" in caplog.text


@pytest.fixture
def echoing_server(loop: _Loop, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answers ``101`` but echoes the whole subprotocol offer back."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        fields = dict(
            (name.strip().lower(), value.strip())
            for name, _, value in (line.partition(":") for line in request.split("\r\n")[1:])
            if name
        )
        accept = base64.b64encode(
            hashlib.sha1(
                (fields["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        head = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept}",
            f"Sec-WebSocket-Protocol: {fields['sec-websocket-protocol']}",
        ]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode())
        await writer.drain()
        writer.close()

    async def start() -> Any:
        return await asyncio.start_server(handle, "127.0.0.1", 0)

    server = loop.run(start())
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, f"http://127.0.0.1:{port}")
    yield
    server.close()
    loop.run(server.wait_closed())


def _assert_masked(exc: ComfyError) -> None:
    assert exc.code == "realtime_upgrade_failed"
    assert "comfy-bearer.***" in str(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _SECRET_TOKEN not in rendered


def test_echoed_offer_does_not_leak_the_token_sync(echoing_server: None) -> None:
    with Comfy(api_key=_KEY) as client, pytest.raises(ComfyError) as info:
        client.realtime.connect("google/genie-4", id_token=_SECRET_TOKEN)
    _assert_masked(info.value)


async def test_echoed_offer_does_not_leak_the_token_async(echoing_server: None) -> None:
    async with AsyncComfy(api_key=_KEY) as client:
        with pytest.raises(ComfyError) as info:
            await client.realtime.connect("google/genie-4", id_token=_SECRET_TOKEN)
    _assert_masked(info.value)
