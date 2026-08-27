"""The clients integrators import: :class:`Comfy` (sync) and :class:`AsyncComfy`.

Both expose the same surface — ``assets`` / ``workflows`` / ``jobs`` constructor
namespaces, the ``models`` namespace, plus ``submit`` / ``run`` — over a shared
sans-IO core. Every namespace is bound to the client's own transport, so they
share its credentials, base URL, connection pool and timeout. Only the
awaiting methods are duplicated; the rules (idempotency, 429 backoff, asset
materialization, UI-format detection) live in ``_core`` and are called from both.

Both clients target Comfy Cloud. Another deployment — a self-hosted proxy or a
serverless one — is selected through the ``COMFY_BASE_URL`` environment
variable; there is no base-URL constructor parameter.

``client.models`` is the exception, and deliberately so: model runs are
model-ID-addressed routes on **Comfy Router**, a different host, so they follow
``COMFY_ROUTER_BASE_URL`` (default ``https://api.comfy.org``) instead. One
variable pointed at both surfaces would send jobs to Router or model runs to
the v2 API; neither serves the other's routes. The credential is the same one
either way — this client's own bearer token, attached to both of its configured
origins and to no third one.

Credentials resolve in a fixed order at construction: the explicit ``api_key``
argument, then the ``COMFY_API_KEY`` environment variable, then — targeting
Comfy Cloud, which always requires a key — a local :class:`MissingApiKey`
naming that variable, raised before any request rather than surfacing as a
server 401 on the first call. A deployment named by ``COMFY_BASE_URL`` may
have no auth at all (a self-hosted ComfyUI behind the API proxy), so there an
unresolved key stays valid and means "send no credentials". That constructor
key is this client's own credential (sent as the ``Authorization`` bearer
token) and is unrelated to the ``api_key`` accepted by ``submit``/``run``,
which authenticates partner (API) nodes embedded in a workflow (e.g. Gemini)
and is sent in the request body instead.

The key itself is write-only from the outside: it is never logged, never
rendered by a client's ``repr``/``str`` (which report only *whether* one is
attached), and never placed in an exception message.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit

from comfy_low.errors import ApiError
from comfy_low.transport import ROUTER_BASE_URL, AsyncComfyLow, ComfyLow, origin

from . import _core
from .assets import AssetFactory, AsyncAssetFactory
from .exceptions import MissingApiKey, WorkflowFormatUi, to_sdk_error
from .jobs import AsyncJob, AsyncJobFactory, Job, JobFactory
from .models import AsyncModels, Models
from .retry import DEFAULT_RETRY, RetryPolicy
from .workflows import Workflow, WorkflowFactory

# How long to keep retrying a full queue before giving up (seconds).
_QUEUE_RETRY_BUDGET = 60.0
#: Base URL of the hosted Comfy Cloud deployment — where a client points by default.
COMFY_CLOUD_BASE_URL = "https://cloud.comfy.org"
#: Environment variable that redirects a client at another deployment.
BASE_URL_ENV_VAR = "COMFY_BASE_URL"
#: Base URL of Comfy Router — where ``client.models`` sends its requests. A
#: *separate* target from :data:`COMFY_CLOUD_BASE_URL`: the ``/api/v2`` surface
#: serves jobs and assets, Router serves the model-ID-addressed invocation
#: routes, and they are different hosts. The literal lives in
#: :mod:`comfy_low.transport` (the layer that owns the wire) and is re-exported
#: here so it sits beside the constant it is most often confused with.
COMFY_ROUTER_BASE_URL = ROUTER_BASE_URL
#: Environment variable that redirects *model runs* at another Router
#: deployment. Deliberately a second variable rather than a reuse of
#: :data:`BASE_URL_ENV_VAR`: one variable pointed at both surfaces would send
#: jobs to Router, or model runs to the v2 API, and neither serves the other's
#: routes. Spelled the same as the TypeScript SDK's, so a test environment sets
#: one variable for both.
ROUTER_BASE_URL_ENV_VAR = "COMFY_ROUTER_BASE_URL"
#: Environment variable read when no ``api_key`` is passed to the constructor.
API_KEY_ENV_VAR = "COMFY_API_KEY"

_DEFAULT_RETRY_AFTER = 2
_now = time.monotonic


def _retry_delay(exc: ApiError, deadline: float) -> float | None:
    """Return a bounded 429 retry delay, or ``None`` when the error should surface."""
    if exc.http_status != 429:
        return None
    if exc.retry_after is None:
        # The spec requires Retry-After on deployment_not_ready. Keep the
        # fallback only for legacy queue_full responses that omit it.
        if exc.code != "queue_full":
            return None
        raw_delay = _DEFAULT_RETRY_AFTER
    else:
        raw_delay = exc.retry_after
    remaining = deadline - _now()
    if remaining <= 0:
        return None
    return max(0.0, min(raw_delay, remaining))


def _resolve_env_url(var: str, default: str) -> str:
    """``var``'s value if it names a usable target, else ``default``.

    Read per construction rather than at import so a process can point
    successive clients at different deployments. An unset-or-blank variable
    means the default, so ``COMFY_BASE_URL=`` in a shell profile or ``.env``
    is not an error.

    Shared by both targets so the two cannot validate differently — a rule that
    held for the v2 base URL and not for the Router one would be a rule nobody
    could state.
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    parsed = urlsplit(raw)
    try:
        # urlsplit defers the port check, so a non-numeric or out-of-range one
        # raises only when .port is read — do it here rather than let httpx
        # fail later with a murkier message.
        _port = parsed.port
        # A query or fragment would land in the middle of every request URL,
        # since the transport builds those by appending the API path.
        valid = (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            f"{var} must be an http(s) URL with no query or fragment "
            f"(e.g. 'http://127.0.0.1:8189'); got {raw!r}"
        )
    return raw


def _resolve_base_url() -> str:
    """Comfy Cloud, unless ``COMFY_BASE_URL`` names another deployment."""
    return _resolve_env_url(BASE_URL_ENV_VAR, COMFY_CLOUD_BASE_URL)


def _resolve_router_base_url() -> str:
    """Comfy Router, unless ``COMFY_ROUTER_BASE_URL`` names another one.

    Same validation as :func:`_resolve_base_url`, and the same
    read-per-construction rule. The trailing slash is stripped here as well as
    in the transport, because this value is *concatenated* with a path that
    already starts with ``/`` — ``https://api.comfy.org//v1/models/...`` is a
    different path to an origin server than the one the spec declares.
    """
    return _resolve_env_url(ROUTER_BASE_URL_ENV_VAR, COMFY_ROUTER_BASE_URL).rstrip("/")


def _same_deployment(url: str, other: str) -> bool:
    """Whether two base URLs name the same deployment.

    Compared by normalized origin (scheme, host, effective port — shared with
    the transport's same-origin credential rule) plus path, rather than by
    string. ``https://cloud.comfy.org:443/`` is Comfy Cloud with its default
    port written out, and reading it as *some other* deployment would hand the
    caller a keyless client and a server 401 on the first request instead of
    the local error this module promises.

    The path is part of the comparison because a deployment mounted under the
    same host (``https://cloud.comfy.org/self-hosted``) is a different target,
    and the keyless carve-out has to keep applying to it.
    """
    return (origin(url), urlsplit(url).path.rstrip("/")) == (
        origin(other),
        urlsplit(other).path.rstrip("/"),
    )


def _resolve_api_key(explicit: str | None, base_url: str) -> str | None:
    """The explicit argument, then ``COMFY_API_KEY``, then a clear local error.

    Read per construction (like the base URL) so one process can build
    successive clients under different credentials. Surrounding whitespace is
    stripped and a blank value counts as unset at either source, so
    ``COMFY_API_KEY=`` in a shell profile — or a key read out of a file with a
    trailing newline — behaves the way it looks.

    Comfy Cloud always requires a key, so exhausting both sources there raises
    :class:`~comfy_sdk.exceptions.MissingApiKey` *here*, with no network call
    attempted: a missing credential reported as a server 401 sends the caller
    looking at their key's validity instead of its absence. A deployment named
    by ``COMFY_BASE_URL`` may legitimately have none (a self-hosted ComfyUI
    behind the API proxy), so there an unresolved key is not an error and keeps
    its documented meaning — send no credentials at all.
    """
    for candidate in (explicit, os.environ.get(API_KEY_ENV_VAR)):
        if candidate and candidate.strip():
            return candidate.strip()
    if _same_deployment(base_url, COMFY_CLOUD_BASE_URL):
        raise MissingApiKey(
            f"no API key: pass api_key=... to the client, or set {API_KEY_ENV_VAR} in the "
            f"environment. Comfy Cloud ({COMFY_CLOUD_BASE_URL}) requires one; set "
            f"{BASE_URL_ENV_VAR} to target a deployment that does not.",
            code="missing_api_key",
        )
    return None


def _guard_ui_format(workflow: Workflow) -> None:
    if _core.looks_like_ui_format(workflow.json):
        raise WorkflowFormatUi(
            "workflow is in UI-export format (nodes/links/last_node_id); submit "
            "the API-format graph instead",
            code="workflow_format_ui",
            http_status=422,
        )


class Comfy:
    """Synchronous Comfy API v2 client.

    Targets Comfy Cloud, or whatever deployment ``COMFY_BASE_URL`` names.
    ``api_key`` is keyword-only so a pre-``COMFY_BASE_URL`` positional URL
    fails loudly instead of being read as a key. Omit it to fall back to
    ``COMFY_API_KEY``; against Comfy Cloud, neither raises
    :class:`~comfy_sdk.exceptions.MissingApiKey` at construction.

    ``retry`` is the policy ``client.models`` calls fail under —
    :data:`~comfy_sdk.retry.DEFAULT_RETRY` unless replaced, and
    :data:`~comfy_sdk.retry.NO_RETRY` to make every call exactly one attempt.
    It does not govern ``submit``/``run``, whose 429 handling follows the
    server's own ``Retry-After`` instead.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        base_url = _resolve_base_url()
        key = _resolve_api_key(api_key, base_url)
        self._low = ComfyLow(
            base_url,
            key,
            timeout=timeout,
            client_info=client_info,
            router_base_url=_resolve_router_base_url(),
        )
        self.assets = AssetFactory(self._low)
        self.workflows = WorkflowFactory()
        self.jobs = JobFactory(self._low)
        #: ``client.models`` — the model namespace, sharing this client's
        #: transport (credentials, base URL, connection pool, timeout) and its
        #: retry policy.
        self.models = Models(self._low, retry)

    def close(self) -> None:
        """Release the underlying HTTP connection pool.

        Prefer the context-manager form (``with Comfy(...) as client:``), which
        calls this for you. Job handles created from this client cannot be used
        afterwards.
        """
        self._low.close()

    def __enter__(self) -> Comfy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        """Target and *whether* a key is attached — never the key itself.

        Explicit rather than inherited: the default ``object`` repr happens not
        to leak, but a client is exactly the object that ends up in a traceback,
        a debugger frame, or a CI log, so what it renders is stated here on
        purpose instead of left to whoever edits this class next.
        """
        return (
            f"{type(self).__name__}(base_url={self._low.safe_base_url!r}, "
            f"authenticated={self._low.authenticated})"
        )

    def _materialize(self, workflow: Workflow) -> dict[str, Any]:
        """Commit every embedded asset handle and substitute ``core/ASSET`` refs."""
        handles = _core.find_asset_handles(workflow.json)
        refs: dict[int, dict[str, Any]] = {}
        for h in handles:
            h.commit()
            refs[id(h)] = h.as_reference()
        return _core.substitute_asset_handles(workflow.json, refs)

    def submit(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        """Submit a workflow. Retries any 429 that carries ``Retry-After``.

        Sends an auto-generated ``Idempotency-Key`` so the server rejects an
        accidental exact resend of *this* request (``422 idempotency_key_reuse``)
        instead of creating a duplicate job. Each call mints a fresh key, so
        calling ``submit()`` again is a distinct submission — to make a retry
        idempotent, pass an explicit ``idempotency_key`` and reuse it. Note a
        reused key is *rejected*, not replayed: on reuse, catch the error and
        poll/list for the job the first attempt already created.

        ``api_key`` authenticates partner (API) nodes embedded in the workflow
        (e.g. Gemini) — unrelated to idempotency and unrelated to the bearer
        token this client was constructed with. It is never persisted or
        logged by the SDK, and is sent as ``extra_data.api_key_comfy_org``
        only when supplied; omitted from the request entirely otherwise.
        """
        _guard_ui_format(workflow)
        graph = self._materialize(workflow)
        key = idempotency_key or _core.new_idempotency_key()
        extra_data = _core.extra_data_for(api_key)
        deadline = _now() + _QUEUE_RETRY_BUDGET
        while True:
            try:
                model = self._low.post_jobs(graph, idempotency_key=key, extra_data=extra_data)
                return Job(self._low, model)
            except ApiError as exc:
                err = to_sdk_error(exc)
                delay = _retry_delay(exc, deadline)
                if delay is None:
                    raise err from exc
                time.sleep(delay)
                continue

    def run(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> Job:
        """Submit, then poll to terminal (authoritative). Raises on failure."""
        job = self.submit(workflow, api_key=api_key)
        return job.result() if timeout is None else _run_with_timeout(job, timeout)


def _run_with_timeout(job: Job, timeout: float) -> Job:
    from ._core import SUCCESS
    from .exceptions import JobFailed

    job.wait(timeout=timeout)
    if job.status != SUCCESS:
        raise JobFailed(f"job {job.id} ended {job.status}", error=job.error)
    return job


class AsyncComfy:
    """Asynchronous Comfy API v2 client — mirrors :class:`Comfy`.

    Same credential resolution (explicit ``api_key`` → ``COMFY_API_KEY`` →
    :class:`~comfy_sdk.exceptions.MissingApiKey` on Comfy Cloud) and the same
    key-free ``repr``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        base_url = _resolve_base_url()
        key = _resolve_api_key(api_key, base_url)
        self._low = AsyncComfyLow(
            base_url,
            key,
            timeout=timeout,
            client_info=client_info,
            router_base_url=_resolve_router_base_url(),
        )
        self.assets = AsyncAssetFactory(self._low)
        self.workflows = WorkflowFactory()
        self.jobs = AsyncJobFactory(self._low)
        #: Async counterpart of :attr:`Comfy.models`, on this client's transport.
        self.models = AsyncModels(self._low, retry)

    async def aclose(self) -> None:
        """Async :meth:`Comfy.close`. Prefer ``async with AsyncComfy(...)``."""
        await self._low.aclose()

    async def __aenter__(self) -> AsyncComfy:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        """Key-free, exactly as :meth:`Comfy.__repr__`."""
        return (
            f"{type(self).__name__}(base_url={self._low.safe_base_url!r}, "
            f"authenticated={self._low.authenticated})"
        )

    async def _materialize(self, workflow: Workflow) -> dict[str, Any]:
        handles = _core.find_asset_handles(workflow.json)
        refs: dict[int, dict[str, Any]] = {}
        for h in handles:
            await h.commit()
            refs[id(h)] = await h.as_reference()
        return _core.substitute_asset_handles(workflow.json, refs)

    async def submit(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncJob:
        """Mirrors :meth:`Comfy.submit` — see there for ``api_key`` details."""
        import asyncio

        _guard_ui_format(workflow)
        graph = await self._materialize(workflow)
        key = idempotency_key or _core.new_idempotency_key()
        extra_data = _core.extra_data_for(api_key)
        deadline = _now() + _QUEUE_RETRY_BUDGET
        while True:
            try:
                model = await self._low.post_jobs(graph, idempotency_key=key, extra_data=extra_data)
                return AsyncJob(self._low, model)
            except ApiError as exc:
                err = to_sdk_error(exc)
                delay = _retry_delay(exc, deadline)
                if delay is None:
                    raise err from exc
                await asyncio.sleep(delay)
                continue

    async def run(
        self,
        workflow: Workflow,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> AsyncJob:
        """Async :meth:`Comfy.run` — submit, then poll to terminal. Raises on failure."""
        from ._core import SUCCESS
        from .exceptions import JobFailed

        job = await self.submit(workflow, api_key=api_key)
        if timeout is None:
            return await job.result()
        await job.wait(timeout=timeout)
        if job.status != SUCCESS:
            raise JobFailed(f"job {job.id} ended {job.status}", error=job.error)
        return job
