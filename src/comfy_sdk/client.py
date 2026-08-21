"""The clients integrators import: :class:`Comfy` (sync) and :class:`AsyncComfy`.

Both expose the same surface — ``assets`` / ``workflows`` / ``jobs`` constructor
namespaces plus ``submit`` / ``run`` — over a shared sans-IO core. Only the
awaiting methods are duplicated; the rules (idempotency, 429 backoff, asset
materialization, UI-format detection) live in ``_core`` and are called from both.

Both clients target Comfy Cloud. Another deployment — a self-hosted proxy or a
serverless one — is selected through the ``COMFY_BASE_URL`` environment
variable; there is no base-URL constructor parameter.

Per-surface key behavior is inherited from ``comfy_low``: pass ``api_key`` to
the constructor for Comfy Cloud / serverless; leave it unset for a self-hosted
proxy that has no auth (no credentials are then sent). That constructor key is
this client's own credential (sent as the ``Authorization`` bearer token) and
is unrelated to the ``api_key`` accepted by ``submit``/``run``, which
authenticates partner (API) nodes embedded in a workflow (e.g. Gemini) and is
sent in the request body instead.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit

from comfy_low.errors import ApiError
from comfy_low.transport import AsyncComfyLow, ComfyLow

from . import _core
from .assets import AssetFactory, AsyncAssetFactory
from .exceptions import WorkflowFormatUi, to_sdk_error
from .jobs import AsyncJob, AsyncJobFactory, Job, JobFactory
from .workflows import Workflow, WorkflowFactory

# How long to keep retrying a full queue before giving up (seconds).
_QUEUE_RETRY_BUDGET = 60.0
#: Base URL of the hosted Comfy Cloud deployment — where a client points by default.
COMFY_CLOUD_BASE_URL = "https://cloud.comfy.org"
#: Environment variable that redirects a client at another deployment.
BASE_URL_ENV_VAR = "COMFY_BASE_URL"

_DEFAULT_RETRY_AFTER = 2


def _resolve_base_url() -> str:
    """Comfy Cloud, unless ``COMFY_BASE_URL`` names another deployment.

    Read per construction rather than at import so a process can point
    successive clients at different deployments. An unset-or-blank variable
    means Comfy Cloud, so ``COMFY_BASE_URL=`` in a shell profile or ``.env``
    is not an error.
    """
    raw = os.environ.get(BASE_URL_ENV_VAR, "").strip()
    if not raw:
        return COMFY_CLOUD_BASE_URL
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
            f"{BASE_URL_ENV_VAR} must be an http(s) URL with no query or fragment "
            f"(e.g. 'http://127.0.0.1:8189'); got {raw!r}"
        )
    return raw


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
    fails loudly instead of being read as a key.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
    ) -> None:
        self._low = ComfyLow(_resolve_base_url(), api_key, timeout=timeout, client_info=client_info)
        self.assets = AssetFactory(self._low)
        self.workflows = WorkflowFactory()
        self.jobs = JobFactory(self._low)

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
        deadline = time.monotonic() + _QUEUE_RETRY_BUDGET
        while True:
            try:
                model = self._low.post_jobs(graph, idempotency_key=key, extra_data=extra_data)
                return Job(self._low, model)
            except ApiError as exc:
                # Disambiguated by status + Retry-After, not `code` alone
                # (e.g. `deployment_not_ready`); a bare `queue_full` 429 (no
                # header) must still retry on the default pause — dropping
                # that fallback is the regression this predicate already hit.
                retryable = exc.http_status == 429 and (
                    exc.retry_after is not None or exc.code == "queue_full"
                )
                remaining = deadline - time.monotonic()
                if retryable and remaining > 0:
                    raw_delay = (
                        exc.retry_after if exc.retry_after is not None else _DEFAULT_RETRY_AFTER
                    )
                    # Clamp: a server-supplied Retry-After is untrusted input —
                    # never sleep past the retry budget, and never negative.
                    delay = max(0.0, min(raw_delay, remaining))
                    time.sleep(delay)
                    continue
                raise to_sdk_error(exc) from exc

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
    """Asynchronous Comfy API v2 client — mirrors :class:`Comfy`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = 30.0,
        client_info: str | None = None,
    ) -> None:
        self._low = AsyncComfyLow(
            _resolve_base_url(), api_key, timeout=timeout, client_info=client_info
        )
        self.assets = AsyncAssetFactory(self._low)
        self.workflows = WorkflowFactory()
        self.jobs = AsyncJobFactory(self._low)

    async def aclose(self) -> None:
        """Async :meth:`Comfy.close`. Prefer ``async with AsyncComfy(...)``."""
        await self._low.aclose()

    async def __aenter__(self) -> AsyncComfy:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

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
        deadline = time.monotonic() + _QUEUE_RETRY_BUDGET
        while True:
            try:
                model = await self._low.post_jobs(graph, idempotency_key=key, extra_data=extra_data)
                return AsyncJob(self._low, model)
            except ApiError as exc:
                # See the sync `submit` above for the predicate and clamp.
                retryable = exc.http_status == 429 and (
                    exc.retry_after is not None or exc.code == "queue_full"
                )
                remaining = deadline - time.monotonic()
                if retryable and remaining > 0:
                    raw_delay = (
                        exc.retry_after if exc.retry_after is not None else _DEFAULT_RETRY_AFTER
                    )
                    delay = max(0.0, min(raw_delay, remaining))
                    await asyncio.sleep(delay)
                    continue
                raise to_sdk_error(exc) from exc

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
