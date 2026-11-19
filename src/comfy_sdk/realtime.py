"""Comfy Router realtime sessions (``client.realtime``) over a WebSocket.

Router serves its realtime models over a WebSocket upgrade at
``GET /v2/realtime/{serving}/{model}`` on the Router host
(``COMFY_ROUTER_BASE_URL``). This module builds that URL, attaches the
credential, opens the socket and turns a refused upgrade into the same typed
:class:`~comfy_sdk.router_exceptions.RouterError` the model-run surface raises.
It is deliberately thin: what flows over the socket once it is open is the
model's own protocol, and no codec or event model is layered over it::

    from comfy_sdk import Comfy

    client = Comfy()
    with client.realtime.connect("xai/grok-voice/realtime") as session:
        session.socket.send(msgpack.packb({...}))      # fal sessions: MessagePack
        frame = session.socket.recv()

The WebSocket library is an optional dependency, because ``httpx`` cannot speak
the protocol: install it with ``pip install 'comfy-sdk[realtime]'``.

**Wire format.** A fal-served session (the default) exchanges MessagePack binary
frames, so bring an encoder such as the ``msgpack`` package; a Google-served one
(``google/genie-4``) exchanges ``json.dumps`` text frames.
:attr:`RealtimeSession.wire_format` says which.

**Auth.** With no ``id_token`` the client's API key goes in an ``X-API-Key``
header. With one, the token rides in a ``comfy-bearer.<token>`` subprotocol
entry beside ``comfy-realtime.v1`` and no credential header is sent: the server
reads the subprotocol only when neither ``Authorization`` nor ``X-API-Key`` is
present, and refuses a bearer entry offered without ``comfy-realtime.v1``. No
credential is ever put in the URL: userinfo on ``COMFY_ROUTER_BASE_URL``, or on
a same-origin redirect target, is dropped from the upgrade, since
``websockets`` would turn it into an ``Authorization`` header. A handshake
redirect is followed only within the Router origin; one to another origin is
refused as a ``RouterError`` for its ``3xx`` instead, because the credential
would ride along to the new host.

**Billing.** A session is billed per second from the ``101`` that opens it, and
a workspace may hold one live WebSocket session at a time.

**Refusals.** A refused upgrade answers with an ordinary HTTP status and the
Router error envelope (``detail``, ``error_type``) plus a ``code`` naming the
specific reason, which lands on
:attr:`~comfy_sdk.router_exceptions.RouterError.refusal_code` (each code below
is prefixed ``router.realtime.`` on the wire):

=====  =======================================================================
426    ``upgrade_required`` -- the request was not a WebSocket upgrade
400    ``subprotocol_required`` -- ``comfy-realtime.v1`` was not offered
401    ``unauthorized``
402    ``insufficient_credits``, ``spend_denied``
403    ``model_unavailable``, ``account_blocked``, ``insufficient_scope``,
       ``provider_disabled``
404    ``model_not_allowed``
429    ``session_already_live``, ``concurrency_limit``, ``rate_limited``
       (with ``Retry-After``, on :attr:`~comfy_sdk.router_exceptions.RouterError.retry_after`)
502    ``token_mint_failed``, ``upstream_refused``
503    ``shutting_down``, ``unpriced``, ``*_unavailable``
=====  =======================================================================

**Close codes.** Once open, the server ends a session with one of:

* ``1000`` (:data:`REALTIME_CLOSE_NORMAL`) -- the model finished, or the Google
  session cap was reached.
* ``1008`` (:data:`REALTIME_CLOSE_POLICY_VIOLATION`) -- a client message broke
  the model's rules.
* ``1011`` (:data:`REALTIME_CLOSE_UPSTREAM_ERROR`) -- the model reported an
  error; the reason text is always ``upstream closed the session``.
* ``1012`` (:data:`REALTIME_CLOSE_SERVICE_RESTART`) -- the server is
  restarting; open a new session.

Any other observed code, notably ``1006``, is a bare close with no close frame
(the fal session cap, credits exhausted, a blocked account) and carries no
reason.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from comfy_low.errors import clean_request_id
from comfy_low.transport import AsyncComfyLow, ComfyLow, origin, redact_userinfo

from .exceptions import ComfyError, MissingApiKey
from .router_exceptions import RouterError, error_from_response

#: Always offered first; the server refuses an upgrade without it.
REALTIME_SUBPROTOCOL = "comfy-realtime.v1"
#: Prefix of the subprotocol entry that carries an ``id_token``.
REALTIME_BEARER_SUBPROTOCOL_PREFIX = "comfy-bearer."
#: Path of the upgrade, relative to the Router base URL.
REALTIME_ROUTE_TEMPLATE = "/v2/realtime/{provider}/{model}"

REALTIME_CLOSE_NORMAL = 1000
REALTIME_CLOSE_POLICY_VIOLATION = 1008
REALTIME_CLOSE_UPSTREAM_ERROR = 1011
REALTIME_CLOSE_SERVICE_RESTART = 1012

#: Which relay serves a model on this route, by catalog id. Anything not listed
#: is served by ``"fal"``; ``provider=`` on :meth:`Realtime.connect` overrides.
_REALTIME_SERVING_PROVIDERS: Mapping[str, str] = {
    "google/genie-4": "google",
}
_DEFAULT_SERVING_PROVIDER = "fal"

#: Realtime models Router serves on a different surface than this WebSocket
#: route, with where to go instead. Refused before any socket opens so the
#: caller gets the right pointer rather than a server 404.
_OTHER_SURFACES: Mapping[str, str] = {
    "fal-ai/lingbot-world-2": "WebRTC signalling, POST /v2/realtime/wma/fal",
    "fal-ai/abot-world-0": "WebRTC signalling, POST /v2/realtime/wma/fal",
    "fish/live-3": "LiveKit session minted by POST /v2/realtime/fish/sessions",
}
_NOT_SERVED_PREFIXES = ("reactor/",)

_SEGMENT_RE = re.compile(r"[a-z0-9._-]+")
#: RFC 7230 ``token`` characters -- what one entry of ``Sec-WebSocket-Protocol``
#: may hold. A comma or space in an ``id_token`` would otherwise split it into
#: extra entries of the caller's offer.
_TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_REQUEST_ID_HEADER = "x-comfy-request-id"

_MISSING_WEBSOCKETS = (
    "comfy-sdk realtime needs the 'websockets' package: pip install 'comfy-sdk[realtime]'"
)


def _segments(value: str, what: str) -> list[str]:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str, got {type(value).__name__}")
    segments = value.split("/")
    for segment in segments:
        # `.`/`..` match the character class, so they are refused by name: they
        # would walk the request path rather than name a model.
        if segment in (".", "..") or not _SEGMENT_RE.fullmatch(segment):
            raise ValueError(
                f"{what} segments must be non-empty, match [a-z0-9._-]+ and not be "
                f"'.' or '..'; got {value!r}"
            )
    return segments


def _realtime_path(model: str, provider: str | None = None) -> tuple[str, str]:
    """``(serving provider, path)`` for ``model``, or a local refusal.

    ``model`` is the full catalog id and is interpolated whole after the
    serving segment, so ``xai/grok-voice/realtime`` becomes
    ``/v2/realtime/fal/xai/grok-voice/realtime``.
    """
    if len(_segments(model, "realtime model id")) < 2:
        raise ValueError(
            f"realtime model id must be the catalog id, '{{provider}}/{{model}}[/...]'; "
            f"got {model!r}"
        )
    elsewhere = _OTHER_SURFACES.get(model)
    if elsewhere is not None:
        raise ComfyError(
            f"{model!r} is not served on the realtime WebSocket route; it uses {elsewhere}",
            code="realtime_unsupported_model",
        )
    if model.startswith(_NOT_SERVED_PREFIXES):
        raise ComfyError(
            f"{model!r} is not served by Comfy Router's realtime surface",
            code="realtime_unsupported_model",
        )
    if provider is None:
        serving = _REALTIME_SERVING_PROVIDERS.get(model, _DEFAULT_SERVING_PROVIDER)
    else:
        if len(_segments(provider, "realtime provider")) != 1:
            raise ValueError(f"realtime provider must be a single path segment; got {provider!r}")
        serving = provider
    return serving, REALTIME_ROUTE_TEMPLATE.format(provider=serving, model=model)


def _ws_base(router_base_url: str) -> str:
    if router_base_url.startswith("https://"):
        return "wss://" + router_base_url[len("https://") :]
    if router_base_url.startswith("http://"):
        return "ws://" + router_base_url[len("http://") :]
    # `_Prepared` already refuses any other scheme at construction.
    raise ValueError(f"router_base_url must be an http(s) URL; got {router_base_url!r}")


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _ws_origin(url: str) -> tuple[str, str, int | None]:
    # `origin` knows the http(s) default ports; ws(s) share them.
    scheme, sep, rest = url.partition("://")
    http = {"ws": "http", "wss": "https"}.get(scheme.lower(), scheme)
    return origin(http + sep + rest)


@dataclass(frozen=True)
class _Plan:
    # `url`, `subprotocols` and `headers` carry the credential (the bearer entry,
    # `X-API-Key`), and a plan is a frame local whenever a refusal raises, so
    # they stay out of the repr a locals-capturing error reporter would render.
    url: str = field(repr=False)
    #: `url` with any userinfo redacted: what the session and errors display.
    safe_url: str
    provider: str
    model: str
    subprotocols: list[str] = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    user_agent: str


def _plan(
    low: ComfyLow | AsyncComfyLow,
    model: str,
    id_token: str | None,
    provider: str | None,
    protocols: Sequence[str],
) -> _Plan:
    serving, path = _realtime_path(model, provider)
    # The transport keeps the key and User-Agent private so a `repr` cannot
    # leak the former; this module is the one other reader of both.
    prepared = low._p
    if isinstance(protocols, str):
        # A bare string would otherwise be offered one character at a time.
        raise TypeError("protocols must be a sequence of strings, not a str")
    extra = list(protocols)
    for entry in extra:
        if not isinstance(entry, str) or not _TOKEN_RE.fullmatch(entry):
            # Same rule as `id_token`: a comma or space would split one entry
            # into several of the caller's offer.
            raise ValueError(
                f"protocols entries must be non-empty strings of HTTP token characters; "
                f"got {entry!r}"
            )
        if entry.startswith(REALTIME_BEARER_SUBPROTOCOL_PREFIX):
            raise ValueError(
                f"protocols must not carry a {REALTIME_BEARER_SUBPROTOCOL_PREFIX!r} entry; "
                "pass the token as id_token=..."
            )
    headers: dict[str, str] = {}
    if id_token is not None:
        if not isinstance(id_token, str) or not _TOKEN_RE.fullmatch(id_token):
            raise ValueError(
                "id_token must be a non-empty string of HTTP token characters "
                "(it rides in the Sec-WebSocket-Protocol header)"
            )
        subprotocols = [REALTIME_SUBPROTOCOL, REALTIME_BEARER_SUBPROTOCOL_PREFIX + id_token, *extra]
    else:
        if not prepared.api_key:
            raise MissingApiKey(
                "no credential for a realtime session: construct the client with an API "
                "key (api_key=... or COMFY_API_KEY), or pass id_token=... to connect",
                code="missing_api_key",
            )
        headers["X-API-Key"] = prepared.api_key
        subprotocols = [REALTIME_SUBPROTOCOL, *extra]
    url = _ws_base(prepared.router_base_url) + path
    return _Plan(
        # websockets sends URL userinfo as `Authorization: Basic`, which would
        # both put a credential beside the one this module chose and make the
        # server ignore a bearer subprotocol entry.
        url=_strip_userinfo(url),
        safe_url=redact_userinfo(url),
        provider=serving,
        model=model,
        subprotocols=subprotocols,
        headers=headers,
        user_agent=prepared._user_agent,
    )


def _refusal(exc: Any) -> RouterError:
    """The typed Router error for a ``websockets`` ``InvalidStatus``."""
    response = exc.response
    headers = {str(k): str(v) for k, v in response.headers.raw_items()}
    body: Any = None
    raw = response.body
    if raw:
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            # A deeply nested body exhausts the parser's recursion limit; it is
            # as undecodable as malformed JSON, not a reason to escape untyped.
            body = None
    return error_from_response(response.status_code, headers, body)


def _upgrade_failed(url: str, exc: BaseException) -> ComfyError:
    return ComfyError(
        f"realtime upgrade to {url} failed: {type(exc).__name__}: {exc}",
        code="realtime_upgrade_failed",
    )


def _request_id(socket: Any) -> str | None:
    response = getattr(socket, "response", None)
    if response is None:
        return None
    # `Headers.get` raises on a repeated header (an intermediary adding its
    # own); take the first. Bounded and filtered exactly as on a refusal: the
    # header is server-controlled.
    values = response.headers.get_all(_REQUEST_ID_HEADER)
    return clean_request_id(values[0]) if values else None


def _guard_redirects(connector: Any, url: str) -> None:
    """Make ``connector`` refuse a handshake redirect that leaves ``url``'s origin.

    The ``websockets`` releases that follow redirects resend the handshake's
    headers and subprotocol offer to the target, stripping only
    ``Authorization`` and cookies -- so ``X-API-Key`` and the bearer entry
    would reach another host. Returning the ``3xx`` instead of the new URI
    makes the library raise it, which surfaces as a ``RouterError``. A
    same-origin target has its userinfo dropped, as the base URL does. A
    release with no ``process_redirect`` does not follow redirects at all.
    """
    follow = getattr(connector, "process_redirect", None)
    if follow is None:
        return

    def process_redirect(exc: Exception) -> Exception | str:
        target = follow(exc)
        if not isinstance(target, str):
            return target
        if _ws_origin(target) != _ws_origin(url):
            return exc
        # Userinfo on the target would become `Authorization: Basic` on the
        # next upgrade, exactly as on the base URL.
        return _strip_userinfo(target)

    connector.process_redirect = process_redirect


def _check_selected(socket: Any) -> None:
    # The server chooses among what was offered, which includes the bearer
    # entry; selecting it would put the token on the session (and its repr).
    selected = socket.subprotocol
    if selected is not None and selected.startswith(REALTIME_BEARER_SUBPROTOCOL_PREFIX):
        raise ComfyError(
            "realtime server selected the credential subprotocol entry",
            code="realtime_upgrade_failed",
        )


def _wire_format(provider: str) -> str:
    return "json" if provider == "google" else "msgpack"


@dataclass
class RealtimeSession:
    """An open realtime session. Use as a context manager to close it.

    ``socket`` is the ``websockets.sync.client.ClientConnection``; send and
    receive on it directly. See the module docstring for the wire formats and
    the close codes the server uses.
    """

    #: The open ``websockets.sync.client.ClientConnection``.
    socket: Any
    #: The ``ws(s)://`` URL the session was opened on, userinfo redacted. The
    #: SDK never puts a credential in it.
    url: str
    #: The relay serving the session: ``"fal"`` or ``"google"``.
    provider: str
    #: The catalog id passed to ``connect``.
    model: str
    #: ``"msgpack"`` (binary frames) for fal, ``"json"`` (text frames) for Google.
    wire_format: str
    #: The subprotocol the ``101`` selected (``comfy-realtime.v1`` is always offered).
    subprotocol: str | None
    #: ``X-Comfy-Request-Id`` from the ``101`` response, if it carried one.
    request_id: str | None

    def close(self, code: int = REALTIME_CLOSE_NORMAL, reason: str = "") -> None:
        """Close the WebSocket with ``code`` and ``reason``."""
        self.socket.close(code, reason)

    def __enter__(self) -> RealtimeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class AsyncRealtimeSession:
    """The async twin of :class:`RealtimeSession`, over ``websockets.asyncio``.

    Close it with ``await session.aclose()`` or ``async with``.
    """

    #: The open ``websockets.asyncio.client.ClientConnection``.
    socket: Any
    url: str
    provider: str
    model: str
    wire_format: str
    subprotocol: str | None
    request_id: str | None

    async def aclose(self, code: int = REALTIME_CLOSE_NORMAL, reason: str = "") -> None:
        """Close the WebSocket with ``code`` and ``reason``."""
        await self.socket.close(code, reason)

    async def __aenter__(self) -> AsyncRealtimeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class Realtime:
    """``client.realtime`` -- opens Router realtime sessions (see the module docs)."""

    def __init__(self, low: ComfyLow) -> None:
        self._low = low

    def connect(
        self,
        model: str,
        *,
        id_token: str | None = None,
        provider: str | None = None,
        protocols: Sequence[str] = (),
        open_timeout: float | None = 10.0,
        max_size: int | None = 2**20,
    ) -> RealtimeSession:
        """Open a realtime session on ``model`` (the full catalog id).

        ``id_token`` authenticates with a bearer token in the subprotocol offer
        instead of the client's API key. ``provider`` overrides the serving
        relay this SDK picks for ``model``. ``protocols`` are offered after the
        SDK's own subprotocol entries. ``max_size`` caps one inbound message in
        bytes (``None`` lifts it); a larger one closes the session with ``1009``.

        Raises a :class:`~comfy_sdk.router_exceptions.RouterError` subclass when
        Router refuses the upgrade, :class:`~comfy_sdk.exceptions.ComfyError`
        with code ``realtime_unsupported_model`` for a model served on another
        surface and ``realtime_upgrade_failed`` when the handshake fails any
        other way, and ``ImportError`` when ``websockets`` is not installed.
        """
        plan = _plan(self._low, model, id_token, provider, protocols)
        try:
            from websockets.exceptions import InvalidStatus, WebSocketException
            from websockets.sync import client as ws_client
            from websockets.typing import Subprotocol
        except ImportError as exc:
            raise ImportError(_MISSING_WEBSOCKETS) from exc
        options: dict[str, Any] = {
            "additional_headers": plan.headers,
            "user_agent_header": plan.user_agent,
            "subprotocols": [Subprotocol(p) for p in plan.subprotocols],
            "open_timeout": open_timeout,
            "max_size": max_size,
        }
        try:
            # websockets 17+ follows redirects in the sync client, through the
            # `reconnect` object `connect()` builds; earlier releases have no
            # such object and follow none.
            reconnect = getattr(ws_client, "reconnect", None)
            if reconnect is None:
                socket = ws_client.connect(plan.url, **options)
            else:
                connector = reconnect(plan.url, **options)
                _guard_redirects(connector, plan.url)
                socket = connector.connect()
        except InvalidStatus as exc:
            raise _refusal(exc) from None
        except (WebSocketException, OSError, TimeoutError, asyncio.TimeoutError, ValueError) as exc:
            raise _upgrade_failed(plan.safe_url, exc) from exc
        # The session owns the socket and closes it, which is what the context
        # manager form promises; entering it says so. websockets 17.1+ otherwise
        # warns that a bare `connect()` result is used outside `with`.
        socket.__enter__()
        try:
            _check_selected(socket)
            return RealtimeSession(
                socket=socket,
                url=plan.safe_url,
                provider=plan.provider,
                model=plan.model,
                wire_format=_wire_format(plan.provider),
                subprotocol=socket.subprotocol,
                request_id=_request_id(socket),
            )
        except BaseException:
            # Nothing else holds the socket, and an open one is billed.
            socket.close()
            raise


class AsyncRealtime:
    """``AsyncComfy().realtime`` -- mirrors :class:`Realtime`, awaitable."""

    def __init__(self, low: AsyncComfyLow) -> None:
        self._low = low

    async def connect(
        self,
        model: str,
        *,
        id_token: str | None = None,
        provider: str | None = None,
        protocols: Sequence[str] = (),
        open_timeout: float | None = 10.0,
        max_size: int | None = 2**20,
    ) -> AsyncRealtimeSession:
        """Mirrors :meth:`Realtime.connect` -- see there."""
        plan = _plan(self._low, model, id_token, provider, protocols)
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import InvalidStatus, WebSocketException
            from websockets.typing import Subprotocol
        except ImportError as exc:
            raise ImportError(_MISSING_WEBSOCKETS) from exc
        try:
            connector = connect(
                plan.url,
                additional_headers=plan.headers,
                user_agent_header=plan.user_agent,
                subprotocols=[Subprotocol(p) for p in plan.subprotocols],
                open_timeout=open_timeout,
                max_size=max_size,
            )
            _guard_redirects(connector, plan.url)
            socket = await connector
        except InvalidStatus as exc:
            raise _refusal(exc) from None
        except (WebSocketException, OSError, TimeoutError, asyncio.TimeoutError, ValueError) as exc:
            raise _upgrade_failed(plan.safe_url, exc) from exc
        try:
            _check_selected(socket)
            return AsyncRealtimeSession(
                socket=socket,
                url=plan.safe_url,
                provider=plan.provider,
                model=plan.model,
                wire_format=_wire_format(plan.provider),
                subprotocol=socket.subprotocol,
                request_id=_request_id(socket),
            )
        except BaseException:
            await socket.close()
            raise
