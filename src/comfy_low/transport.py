"""Thin ``comfy_low`` transport over httpx — sync and async.

One function per ``operationId`` in ``spec/openapi.yaml``, plus the mandatory
escape hatches the hand-written ``comfy_sdk`` layer builds on:

* **raw response access** — ``raw_request`` returns the fully-read ``httpx.Response``;
* **unbuffered / streaming bodies** — ``open`` yields a streaming response, and
  ``post_assets`` streams its multipart body instead of buffering the file;
* **all headers** — every method that needs them exposes the response headers;
* **per-request timeout / abort** — every method takes ``timeout`` and the raw
  httpx cancellation applies.

One binding is *not* backed by an ``operationId``: ``post_model_run``. The
vendored contract declares no model routes yet, so it is hand-written against
the agreed wire shape, kept out of ``comfy_low.OPERATION_IDS``, and confined to
``model_run_request`` / ``_MODEL_RUN_PATH`` so vendoring the real route later is
a one-place change.

This layer contains no orchestration, retries, hashing, or reconnection — those
live in ``comfy_sdk``.
"""

from __future__ import annotations

import asyncio
import platform
import secrets
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from . import _multipart
from .errors import ApiError, error_from_envelope
from .models import Asset, Job, JobWorkflowResponse
from .sse import RawEvent, SSEDecoder

_API = "/api/v2"
_UNSET = object()

# SSE read-idle timeout. The server sends keepalive comments roughly every 15s,
# so no data at all for ~3x that means the connection has silently stalled
# ("zombie": open but delivering nothing) rather than being legitimately idle.
# Applying a read timeout makes that case raise a ReadTimeout, which
# comfy_sdk.Job.events() catches and turns into a poll/reconnect — instead of
# blocking iter_lines() forever. (Pass timeout=None to opt out of the timeout.)
_SSE_IDLE_TIMEOUT = httpx.Timeout(10.0, read=45.0)

#: Default timeout for a model run. A run is *awaited server-side*: the server
#: holds the connection until the generation is complete, polling the upstream
#: provider itself when that provider is submit/poll. So the client has to be
#: willing to wait minutes, not the tens of seconds a normal API call gets —
#: the client's own default (30s) would abort a perfectly healthy generation.
#: ``connect`` stays short: an unreachable host is not a slow generation.
MODEL_RUN_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

#: Route for a model run. NOT an ``operationId`` from ``spec/openapi.yaml`` —
#: the vendored v2 contract declares no model routes, so this binding is
#: hand-written and is deliberately absent from ``comfy_low.OPERATION_IDS``
#: (the spec-coverage test asserts that set equals the spec's, exactly).
#: Everything about the wire shape is confined to this constant and
#: :func:`model_run_request` so it is one place to reconcile when the route is
#: vendored into the spec and the models are regenerated from it.
_MODEL_RUN_PATH = "/models/run"

_DEFAULT_PORTS = {"http": 80, "https": 443}


def model_run_request(
    model: str,
    arguments: Mapping[str, Any],
    idempotency_key: str | None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Sans-IO ``(path, json_body, headers)`` for one model run.

    The model id travels in the *body*, not the path: ids are commonly
    provider-namespaced and contain ``/`` (``vendor/family/variant``), which in
    a path segment needs percent-encoding that intermediaries normalize
    inconsistently. A named body field also leaves room for sibling fields
    later without moving the route.

    ``arguments`` is copied into a plain dict so any ``Mapping`` is accepted and
    the caller's object is never handed to the JSON encoder directly.
    """
    body: dict[str, Any] = {"model": model, "arguments": dict(arguments)}
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return _MODEL_RUN_PATH, body, headers


def _build_user_agent(client_info: str | None) -> str:
    """SDK identity sent on every request. This is request metadata (not
    telemetry — no phone-home), so adoption is measurable server-side from
    request logs. An optional caller-set ``app/{client_info}`` token lets an
    integration attribute its own traffic.
    """
    try:
        ver = _pkg_version("comfy-sdk")
    except PackageNotFoundError:
        ver = "0"
    ua = (
        f"comfy-sdk-python/{ver} "
        f"({platform.python_implementation()} {sys.version_info.major}.{sys.version_info.minor})"
    )
    if client_info:
        # A caller-set token goes verbatim into a header value; reject CR/LF so
        # it can never split/inject headers (httpx would reject it anyway, but
        # fail fast with a clear message at construction).
        if "\r" in client_info or "\n" in client_info:
            raise ValueError("client_info must not contain CR or LF characters")
        ua += f" app/{client_info}"
    return ua


def _retry_after(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def origin(url: str) -> tuple[str, str, int | None]:
    """Normalized ``(scheme, host, port)`` — the parts that define same-origin.

    Public because it is the single definition of "same target" in the SDK: the
    transport decides here whether a URL may carry the bearer token, and
    ``comfy_sdk.client`` compares against it to recognize Comfy Cloud however
    the caller spelled it. Two spellings that differ only in case or in an
    explicitly written default port are the same origin.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    port = parts.port
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (scheme, (parts.hostname or "").lower(), port)


def redact_userinfo(url: str) -> str:
    """``url`` with any userinfo replaced by ``***`` — the form safe to render.

    A base URL may legitimately carry credentials in its netloc
    (``https://user:token@proxy.example`` for an authenticating proxy), and a
    client is exactly the object that ends up in a traceback, a debugger frame
    or a CI log. So every ``repr`` renders this form while the URL the
    transport actually requests against keeps the userinfo intact — the
    credential is hidden from display, not taken away from the caller.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))


class _Prepared:
    """Sans-IO request building shared by both transports."""

    def __init__(self, base_url: str, api_key: str | None, client_info: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        #: ``base_url`` with any userinfo redacted — what every ``repr`` shows.
        self.safe_base_url = redact_userinfo(self.base_url)
        self._base_origin = origin(self.base_url)
        parts = urlsplit(self.base_url)
        self._origin_url = f"{parts.scheme}://{parts.netloc}"
        self._user_agent = _build_user_agent(client_info)

    def __repr__(self) -> str:
        # This object holds the bearer token, so its repr is written out rather
        # than inherited: `authenticated` reports only whether one is set, and
        # the base URL is the redacted form so a credential embedded *in it*
        # does not leak either.
        return (
            f"{type(self).__name__}(base_url={self.safe_base_url!r}, "
            f"authenticated={bool(self.api_key)})"
        )

    def url(self, path: str) -> str:
        # A server link (job.urls.*, marked by containing /api/) already carries
        # the server's mount prefix, so it resolves against the origin — joining
        # it to base_url would double the prefix on a prefix-mounted surface.
        if path.startswith("http"):
            return path
        if path.startswith("/") and "/api/" in path:
            return self._origin_url + path
        return self.base_url + _API + path

    def headers(self, url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        # User-Agent goes on every request (all origins — it is not a
        # credential); a caller-supplied one in ``extra`` still wins.
        h: dict[str, str] = {"User-Agent": self._user_agent}
        # Only authenticate when a key is set: a local proxy fronts a ComfyUI
        # with no auth, so we never leak credentials it does not want. And only
        # attach it when the resolved request URL is same-origin as base_url:
        # server-returned absolute follow-up links (job.urls.self/cancel/events)
        # must not carry the key to a different scheme/host/port. Relative paths
        # are always resolved under base_url, so they are unaffected.
        if self.api_key and origin(url) == self._base_origin:
            h["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            h.update(extra)
        return h

    def parse_or_raise(self, resp: httpx.Response, ok: tuple[int, ...]) -> dict[str, Any]:
        if resp.status_code in ok:
            if resp.content:
                return resp.json()
            return {}
        body: dict[str, Any] | None
        try:
            body = resp.json()
        except Exception:
            body = None
        raise error_from_envelope(resp.status_code, body, retry_after=_retry_after(resp))


def parse_expiry(url: str) -> datetime | None:
    """Expiry of a GCS V4 signed URL from its ``X-Goog-Date``/``X-Goog-Expires``
    query params — mirrors the server's own signed-URL-expiry computation.

    Returns ``None`` when either param is missing or malformed, which is the
    normal case for a same-origin content URL (self-hosted backend) rather than
    a signed one.
    """
    query = parse_qs(urlsplit(url).query)
    date_vals = query.get("X-Goog-Date")
    expires_vals = query.get("X-Goog-Expires")
    if not date_vals or not expires_vals:
        return None
    try:
        issued = datetime.strptime(date_vals[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        seconds = int(expires_vals[0])
        return issued + timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        # A malformed date/expires, or an ``expires`` so large the resulting
        # datetime overflows, is treated as "no expiry" rather than raising.
        return None


def _new_boundary() -> str:
    return "----comfy" + secrets.token_hex(16)


async def _async_multipart_body(chunks: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Bridge the sync multipart chunk generator into an async byte stream.

    ``_multipart.build_multipart`` reads the file with blocking ``read()`` calls
    (it has to — file objects are sync). ``httpx.AsyncClient`` requires an async
    iterable body, so each ``next()`` (which may block on a file read) is run in
    a worker thread via ``asyncio.to_thread`` — that keeps the event loop free
    while still streaming chunk-by-chunk instead of buffering the whole file.
    """
    it = iter(chunks)
    while True:
        chunk = await asyncio.to_thread(next, it, None)
        if chunk is None:
            return
        yield chunk


class ComfyLow:
    """Synchronous protocol bindings."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
    ) -> None:
        self._p = _Prepared(base_url, api_key, client_info)
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    # -- configuration ----------------------------------------------------
    # Read-only views of the settings this transport was built with, so a layer
    # above (a ``comfy_sdk`` client namespace) can report the configuration it
    # shares without reaching into private attributes. Both read through to the
    # live objects, so a later change to either is reflected here.
    @property
    def base_url(self) -> str:
        """Base URL every relative API path is resolved against."""
        return self._p.base_url

    @property
    def safe_base_url(self) -> str:
        """:attr:`base_url` with any userinfo redacted — the form safe to log."""
        return self._p.safe_base_url

    @property
    def timeout(self) -> httpx.Timeout:
        """The httpx client's default timeout. A per-request ``timeout=`` still wins."""
        return self._client.timeout

    @property
    def authenticated(self) -> bool:
        """Whether a credential is attached to same-origin requests.

        The key itself is deliberately not exposed here: this is the read a
        layer above (or a ``repr``) needs, and it cannot leak the credential.
        """
        return bool(self._p.api_key)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.safe_base_url!r}, "
            f"authenticated={self.authenticated})"
        )

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def __enter__(self) -> ComfyLow:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- escape hatches ---------------------------------------------------
    def raw_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: Any = None,
        timeout: Any = _UNSET,
    ) -> httpx.Response:
        """Fully-read raw response — headers, status, and body all accessible."""
        kw: dict[str, Any] = {}
        if timeout is not _UNSET:
            kw["timeout"] = timeout
        url = self._p.url(path)
        return self._client.request(
            method,
            url,
            headers=self._p.headers(url, headers),
            json=json,
            content=content,
            **kw,
        )

    @contextmanager
    def open(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: Any = None,
        timeout: Any = _UNSET,
    ) -> Iterator[httpx.Response]:
        """Streaming (unbuffered) response for binary and SSE bodies."""
        kw: dict[str, Any] = {}
        if timeout is not _UNSET:
            kw["timeout"] = timeout
        url = self._p.url(path)
        with self._client.stream(
            method,
            url,
            headers=self._p.headers(url, headers),
            json=json,
            content=content,
            **kw,
        ) as resp:
            yield resp

    # -- assets -----------------------------------------------------------
    def post_assets(
        self,
        file: BinaryIO,
        content_type: str,
        file_path: str,
        *,
        expected_hash: str | None = None,
        tags: list[str] | None = None,
        idempotency_key: str | None = None,
        file_size: int | None = None,
        expires_in: int | None = None,
        timeout: Any = _UNSET,
    ) -> Asset:
        """POST /api/v2/assets — streaming multipart upload."""
        if file_size is None:
            file_size = _multipart.file_size_of(file)
        fields: list[tuple[str, str]] = [
            ("content_type", content_type),
            ("file_path", file_path),
        ]
        if expected_hash is not None:
            fields.append(("expected_hash", expected_hash))
        if tags:
            # One part per tag — repeating the field name is the multipart/form
            # convention for a list, and a dict would silently drop all but one.
            fields.extend(("tags", t) for t in tags)
        if expires_in is not None:
            fields.append(("expires_in", str(expires_in)))
        boundary = _new_boundary()
        body, length = _multipart.build_multipart(
            boundary,
            fields=fields,
            file_name=file_path,
            file_obj=file,
            file_content_type=content_type,
            file_size=file_size,
        )
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if length is not None:
            headers["Content-Length"] = str(length)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        resp = self.raw_request("POST", "/assets", headers=headers, content=body, timeout=timeout)
        data = self._p.parse_or_raise(resp, (200, 201, 202))
        return Asset.model_validate(data)

    def asset_from_hash(
        self,
        hash: str,
        *,
        file_path: str | None = None,
        tags: list[str] | None = None,
        expires_in: int | None = None,
        timeout: Any = _UNSET,
    ) -> Asset:
        """POST /api/v2/assets/from-hash — dedup mint over existing bytes."""
        payload: dict[str, Any] = {"hash": hash}
        if file_path is not None:
            payload["file_path"] = file_path
        if tags is not None:
            payload["tags"] = tags
        if expires_in is not None:
            payload["expires_in"] = expires_in
        resp = self.raw_request("POST", "/assets/from-hash", json=payload, timeout=timeout)
        data = self._p.parse_or_raise(resp, (200, 201))
        return Asset.model_validate(data)

    def head_asset_by_hash(self, hash: str, *, timeout: Any = _UNSET) -> bool:
        """HEAD /api/v2/assets/by-hash/{hash} — existence probe."""
        resp = self.raw_request("HEAD", f"/assets/by-hash/{hash}", timeout=timeout)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        return bool(self._p.parse_or_raise(resp, (200,)))  # raises typed error

    def get_asset(self, asset_id: str, *, timeout: Any = _UNSET) -> Asset:
        """GET /api/v2/assets/{id} — metadata with a fresh content URL."""
        resp = self.raw_request("GET", f"/assets/{asset_id}", timeout=timeout)
        return Asset.model_validate(self._p.parse_or_raise(resp, (200,)))

    def delete_asset(self, asset_id: str, *, timeout: Any = _UNSET) -> None:
        """DELETE /api/v2/assets/{id} — removes the asset record and its content."""
        resp = self.raw_request("DELETE", f"/assets/{asset_id}", timeout=timeout)
        self._p.parse_or_raise(resp, (204,))

    @contextmanager
    def get_asset_content(
        self,
        asset_id: str,
        *,
        range: tuple[int, int] | None = None,
        timeout: Any = _UNSET,
    ) -> Iterator[httpx.Response]:
        """GET /api/v2/assets/{id}/content — raw, streamed, range-aware body.

        Yields the streaming response (escape hatch); the caller iterates bytes.
        """
        headers: dict[str, str] = {}
        if range is not None:
            headers["Range"] = f"bytes={range[0]}-{range[1]}"
        with self.open(
            "GET", f"/assets/{asset_id}/content", headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code not in (200, 206):
                resp.read()
                self._p.parse_or_raise(resp, (200, 206))
            yield resp

    def get_asset_content_url(
        self, asset_id: str, *, timeout: Any = _UNSET
    ) -> tuple[str, datetime | None]:
        """GET /api/v2/assets/{id}/content without following a redirect.

        A Cloud/serverless backend answers with a redirect to a short-lived
        signed URL (object storage); a self-hosted backend serves the bytes
        inline (200/206), with no redirect at all. Either way this never reads
        the body — only headers are needed to resolve the URL, so a large
        asset's bytes are never pulled through this call.

        ``follow_redirects=False`` is passed per-request (the shared client
        otherwise follows redirects for every other call) because the
        ``Location`` *is* the answer here — following it would fetch bytes we
        don't want, and would also run the client's auth-header logic against
        the redirect target, which must never see our bearer token.
        """
        path = f"/assets/{asset_id}/content"
        url = self._p.url(path)
        kw: dict[str, Any] = {}
        if timeout is not _UNSET:
            kw["timeout"] = timeout
        with self._client.stream(
            "GET", url, headers=self._p.headers(url), follow_redirects=False, **kw
        ) as resp:
            if resp.has_redirect_location:
                location = resp.headers["Location"]
                return location, parse_expiry(location)
            if resp.status_code in (200, 206):
                return url, None
            resp.read()
            self._p.parse_or_raise(resp, (200, 206))
        raise AssertionError("unreachable: parse_or_raise always raises for a non-ok status")

    # -- jobs -------------------------------------------------------------
    def post_jobs(
        self,
        workflow: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        extra_data: dict[str, Any] | None = None,
        timeout: Any = _UNSET,
    ) -> Job:
        """POST /api/v2/jobs.

        ``extra_data`` (e.g. ``{"api_key_comfy_org": "..."}``) is a sibling of
        ``workflow`` in the body, never nested inside it. Omitted from the
        request entirely when ``None`` — the server rejects an empty
        ``extra_data`` object, and a caller with no partner key should never
        send one.
        """
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body: dict[str, Any] = {"workflow": workflow}
        if extra_data:
            body["extra_data"] = extra_data
        resp = self.raw_request(
            "POST",
            "/jobs",
            headers=headers,
            json=body,
            timeout=timeout,
        )
        data = self._p.parse_or_raise(resp, (201,))
        return Job.model_validate(data)

    def get_job(self, job_id_or_url: str, *, timeout: Any = _UNSET) -> Job:
        """GET /api/v2/jobs/{id} (or an absolute self link)."""
        path = job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}"
        resp = self.raw_request("GET", path, timeout=timeout)
        return Job.model_validate(self._p.parse_or_raise(resp, (200,)))

    def get_job_events(self, job_id_or_url: str, *, timeout: Any = _UNSET) -> Iterator[RawEvent]:
        """GET /api/v2/jobs/{id}/events — raw live SSE iterator (escape hatch).

        No reconnection here; a single connection's frames. ``comfy_sdk`` adds the
        reconnect loop. Defaults to a read-idle timeout (``_SSE_IDLE_TIMEOUT``, well
        above the server's keepalive interval) so a silently-stalled connection
        raises instead of blocking forever; pass ``timeout=None`` to disable.
        """
        path = job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}/events"
        headers = {"Accept": "text/event-stream"}
        decoder = SSEDecoder()
        eff_timeout = _SSE_IDLE_TIMEOUT if timeout is _UNSET else timeout
        with self.open("GET", path, headers=headers, timeout=eff_timeout) as resp:
            if resp.status_code != 200:
                resp.read()
                self._p.parse_or_raise(resp, (200,))
            for line in resp.iter_lines():
                yield from decoder.push(line)

    def cancel_job(self, job_id_or_url: str, *, timeout: Any = _UNSET) -> Job:
        """POST /api/v2/jobs/{id}/cancel — idempotent."""
        path = job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}/cancel"
        resp = self.raw_request("POST", path, timeout=timeout)
        return Job.model_validate(self._p.parse_or_raise(resp, (200,)))

    def get_job_workflow(self, job_id_or_url: str, *, timeout: Any = _UNSET) -> JobWorkflowResponse:
        """GET /api/v2/jobs/{id}/workflow — the workflow graph behind a job."""
        path = (
            job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}/workflow"
        )
        resp = self.raw_request("GET", path, timeout=timeout)
        return JobWorkflowResponse.model_validate(self._p.parse_or_raise(resp, (200,)))

    # -- models -----------------------------------------------------------
    def post_model_run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: Any = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """POST /api/v2/models/run — run a model, awaited server-side.

        One request, one response: the server does not answer until the
        generation is complete, so the decoded body *is* the finished result.
        That holds for a submit/poll provider too — the polling happens server
        side, inside this call, which is why the default ``timeout`` is
        :data:`MODEL_RUN_TIMEOUT` rather than the client's own.

        The body is returned verbatim — the provider's native payload, with no
        model class layered over it. This is not a spec operation; see
        :data:`_MODEL_RUN_PATH`.
        """
        path, body, headers = model_run_request(model, arguments, idempotency_key)
        resp = self.raw_request("POST", path, headers=headers, json=body, timeout=timeout)
        return self._p.parse_or_raise(resp, (200, 201))


class AsyncComfyLow:
    """Asynchronous protocol bindings — mirrors :class:`ComfyLow`."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
    ) -> None:
        self._p = _Prepared(base_url, api_key, client_info)
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    # -- configuration (mirrors :class:`ComfyLow`) -------------------------
    @property
    def base_url(self) -> str:
        """Base URL every relative API path is resolved against."""
        return self._p.base_url

    @property
    def safe_base_url(self) -> str:
        """:attr:`base_url` with any userinfo redacted — the form safe to log."""
        return self._p.safe_base_url

    @property
    def timeout(self) -> httpx.Timeout:
        """The httpx client's default timeout. A per-request ``timeout=`` still wins."""
        return self._client.timeout

    @property
    def authenticated(self) -> bool:
        """Whether a credential is attached — mirrors :attr:`ComfyLow.authenticated`."""
        return bool(self._p.api_key)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.safe_base_url!r}, "
            f"authenticated={self.authenticated})"
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncComfyLow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def raw_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: Any = None,
        timeout: Any = _UNSET,
    ) -> httpx.Response:
        kw: dict[str, Any] = {}
        if timeout is not _UNSET:
            kw["timeout"] = timeout
        url = self._p.url(path)
        return await self._client.request(
            method,
            url,
            headers=self._p.headers(url, headers),
            json=json,
            content=content,
            **kw,
        )

    @asynccontextmanager
    async def open(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        content: Any = None,
        timeout: Any = _UNSET,
    ) -> AsyncIterator[httpx.Response]:
        kw: dict[str, Any] = {}
        if timeout is not _UNSET:
            kw["timeout"] = timeout
        url = self._p.url(path)
        async with self._client.stream(
            method,
            url,
            headers=self._p.headers(url, headers),
            json=json,
            content=content,
            **kw,
        ) as resp:
            yield resp

    async def post_assets(
        self,
        file: BinaryIO,
        content_type: str,
        file_path: str,
        *,
        expected_hash: str | None = None,
        tags: list[str] | None = None,
        idempotency_key: str | None = None,
        file_size: int | None = None,
        expires_in: int | None = None,
        timeout: Any = _UNSET,
    ) -> Asset:
        if file_size is None:
            file_size = _multipart.file_size_of(file)
        fields: list[tuple[str, str]] = [
            ("content_type", content_type),
            ("file_path", file_path),
        ]
        if expected_hash is not None:
            fields.append(("expected_hash", expected_hash))
        if tags:
            # One part per tag — see the sync ``post_assets`` for why a dict is wrong.
            fields.extend(("tags", t) for t in tags)
        if expires_in is not None:
            fields.append(("expires_in", str(expires_in)))
        boundary = _new_boundary()
        body, length = _multipart.build_multipart(
            boundary,
            fields=fields,
            file_name=file_path,
            file_obj=file,
            file_content_type=content_type,
            file_size=file_size,
        )
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if length is not None:
            headers["Content-Length"] = str(length)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        # AsyncClient requires an async-iterable body: build_multipart only ever
        # produces a sync generator (file reads are inherently sync), so bridge
        # it — see _async_multipart_body — instead of handing httpx a sync
        # generator, which raises RuntimeError the moment it tries to send.
        resp = await self.raw_request(
            "POST",
            "/assets",
            headers=headers,
            content=_async_multipart_body(body),
            timeout=timeout,
        )
        data = self._p.parse_or_raise(resp, (200, 201, 202))
        return Asset.model_validate(data)

    async def asset_from_hash(
        self,
        hash: str,
        *,
        file_path: str | None = None,
        tags: list[str] | None = None,
        expires_in: int | None = None,
        timeout: Any = _UNSET,
    ) -> Asset:
        payload: dict[str, Any] = {"hash": hash}
        if file_path is not None:
            payload["file_path"] = file_path
        if tags is not None:
            payload["tags"] = tags
        if expires_in is not None:
            payload["expires_in"] = expires_in
        resp = await self.raw_request("POST", "/assets/from-hash", json=payload, timeout=timeout)
        data = self._p.parse_or_raise(resp, (200, 201))
        return Asset.model_validate(data)

    async def head_asset_by_hash(self, hash: str, *, timeout: Any = _UNSET) -> bool:
        resp = await self.raw_request("HEAD", f"/assets/by-hash/{hash}", timeout=timeout)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        return bool(self._p.parse_or_raise(resp, (200,)))

    async def get_asset(self, asset_id: str, *, timeout: Any = _UNSET) -> Asset:
        resp = await self.raw_request("GET", f"/assets/{asset_id}", timeout=timeout)
        return Asset.model_validate(self._p.parse_or_raise(resp, (200,)))

    async def delete_asset(self, asset_id: str, *, timeout: Any = _UNSET) -> None:
        """DELETE /api/v2/assets/{id} — removes the asset record and its content."""
        resp = await self.raw_request("DELETE", f"/assets/{asset_id}", timeout=timeout)
        self._p.parse_or_raise(resp, (204,))

    @asynccontextmanager
    async def get_asset_content(
        self,
        asset_id: str,
        *,
        range: tuple[int, int] | None = None,
        timeout: Any = _UNSET,
    ) -> AsyncIterator[httpx.Response]:
        headers: dict[str, str] = {}
        if range is not None:
            headers["Range"] = f"bytes={range[0]}-{range[1]}"
        async with self.open(
            "GET", f"/assets/{asset_id}/content", headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code not in (200, 206):
                await resp.aread()
                self._p.parse_or_raise(resp, (200, 206))
            yield resp

    async def get_asset_content_url(
        self, asset_id: str, *, timeout: Any = _UNSET
    ) -> tuple[str, datetime | None]:
        """See the sync ``get_asset_content_url`` for the redirect/inline split."""
        path = f"/assets/{asset_id}/content"
        url = self._p.url(path)
        kw: dict[str, Any] = {}
        if timeout is not _UNSET:
            kw["timeout"] = timeout
        async with self._client.stream(
            "GET", url, headers=self._p.headers(url), follow_redirects=False, **kw
        ) as resp:
            if resp.has_redirect_location:
                location = resp.headers["Location"]
                return location, parse_expiry(location)
            if resp.status_code in (200, 206):
                return url, None
            await resp.aread()
            self._p.parse_or_raise(resp, (200, 206))
        raise AssertionError("unreachable: parse_or_raise always raises for a non-ok status")

    async def post_jobs(
        self,
        workflow: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        extra_data: dict[str, Any] | None = None,
        timeout: Any = _UNSET,
    ) -> Job:
        """POST /api/v2/jobs — see the sync ``post_jobs`` for ``extra_data``."""
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body: dict[str, Any] = {"workflow": workflow}
        if extra_data:
            body["extra_data"] = extra_data
        resp = await self.raw_request(
            "POST",
            "/jobs",
            headers=headers,
            json=body,
            timeout=timeout,
        )
        data = self._p.parse_or_raise(resp, (201,))
        return Job.model_validate(data)

    async def get_job(self, job_id_or_url: str, *, timeout: Any = _UNSET) -> Job:
        path = job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}"
        resp = await self.raw_request("GET", path, timeout=timeout)
        return Job.model_validate(self._p.parse_or_raise(resp, (200,)))

    async def get_job_events(
        self, job_id_or_url: str, *, timeout: Any = _UNSET
    ) -> AsyncIterator[RawEvent]:
        # Read-idle timeout by default (see the sync get_job_events); a stalled
        # connection raises so comfy_sdk falls back to poll/reconnect.
        path = job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}/events"
        headers = {"Accept": "text/event-stream"}
        decoder = SSEDecoder()
        eff_timeout = _SSE_IDLE_TIMEOUT if timeout is _UNSET else timeout
        async with self.open("GET", path, headers=headers, timeout=eff_timeout) as resp:
            if resp.status_code != 200:
                await resp.aread()
                self._p.parse_or_raise(resp, (200,))
            async for line in resp.aiter_lines():
                for event in decoder.push(line):
                    yield event

    async def cancel_job(self, job_id_or_url: str, *, timeout: Any = _UNSET) -> Job:
        path = job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}/cancel"
        resp = await self.raw_request("POST", path, timeout=timeout)
        return Job.model_validate(self._p.parse_or_raise(resp, (200,)))

    async def get_job_workflow(
        self, job_id_or_url: str, *, timeout: Any = _UNSET
    ) -> JobWorkflowResponse:
        """Async :meth:`ComfyLow.get_job_workflow`."""
        path = (
            job_id_or_url if _looks_like_path(job_id_or_url) else f"/jobs/{job_id_or_url}/workflow"
        )
        resp = await self.raw_request("GET", path, timeout=timeout)
        return JobWorkflowResponse.model_validate(self._p.parse_or_raise(resp, (200,)))

    # -- models -----------------------------------------------------------
    async def post_model_run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: Any = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """Async :meth:`ComfyLow.post_model_run`."""
        path, body, headers = model_run_request(model, arguments, idempotency_key)
        resp = await self.raw_request("POST", path, headers=headers, json=body, timeout=timeout)
        return self._p.parse_or_raise(resp, (200, 201))


def _looks_like_path(s: str) -> bool:
    return s.startswith("http") or s.startswith("/")


__all__ = ["ComfyLow", "AsyncComfyLow", "ApiError"]
