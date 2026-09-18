"""Idiomatic ``comfy_sdk`` exceptions.

These wrap the protocol-level ``comfy_low.ApiError`` codes with names an
integrator catches directly (``JobFailed``, ``QueueFull``, ...). ``to_sdk_error``
maps a raised ``ApiError`` to the right subclass; anything unmapped stays a
``ComfyError`` carrying the original code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import httpx

from comfy_low.errors import ApiError
from comfy_low.models import JobError


class ComfyError(Exception):
    """Base for every SDK-level error."""

    #: The ``Idempotency-Key`` the failed call was made under. Populated by
    #: :meth:`comfy_sdk.models.Models.run` and
    #: :meth:`comfy_sdk.models.Models.submit` and their async twins, and by
    #: :meth:`comfy_sdk.client.Comfy.submit` /
    #: :meth:`comfy_sdk.client.AsyncComfy.submit` on every failure of the
    #: ``POST /jobs`` attempt itself — and so by the submit phase of
    #: ``Comfy.run`` / ``AsyncComfy.run``. A failure while ``run`` polls the
    #: job afterwards (a ``JobFailed``, a wait timeout) carries none: by then
    #: the job exists and its id is the handle. It is ``None`` everywhere
    #: else: on an operation that sends no key, on an asset upload (which
    #: mints a key per handle and does not record it), and on an exception
    #: constructed by hand. So ``None`` means "this SDK did not record a key
    #: for you", never "no key reached the server": do not infer from it that
    #: a resend is safe.
    #:
    #: What the key is *good for* differs by surface, so read it with the
    #: operation in mind: ``models.run`` and ``models.submit`` send it to a
    #: surface that replays a claimed key, so the key is a handle on the
    #: generation you were already billed for. ``POST /jobs`` instead
    #: *rejects* a reused key with ``422 idempotency_key_reuse``, so on a
    #: ``Comfy.submit`` failure the key is the one this attempt was made
    #: under, not a replay handle: after an ambiguous failure poll or list for
    #: the job the first attempt may have created rather than resubmitting
    #: under it, while a failure the server never saw (a connect failure, an
    #: exhausted ``QueueFull``) leaves the key unclaimed.
    #:
    #: Declared on the base rather than set per subclass so that a bucket this
    #: SDK version has never heard of — which arrives as a bare
    #: :class:`~comfy_sdk.router_exceptions.RouterError` — still carries it.
    idempotency_key: str | None = None

    #: Server-minted id for the call, from ``X-Comfy-Request-Id``, or ``None``
    #: when the response carried no such header (and on a failure with no
    #: response at all). The id a user quotes in a support request.
    request_id: str | None = None

    #: Seconds the server asked the caller to wait before asking again, from
    #: ``Retry-After``, or ``None`` when it named no pace. Carried on the base
    #: because the header is not the throttled buckets' alone — a
    #: ``deadline_exceeded`` ``504`` names the pace at which a replay of the
    #: same ``Idempotency-Key`` may be attempted, and a caller told to wait for
    #: it needs somewhere to read it. :class:`QueueFull` narrows it to a
    #: required ``int``.
    retry_after: int | None = None

    #: ``True`` only on the failure :meth:`comfy_sdk.models.Models.run` re-raises
    #: after a same-key resend was refused — the original failure, with the key
    #: refusal on ``__cause__``. It exists for outer retry wrappers: a wrapper
    #: keyed on ``http_status >= 500`` alone would retry this error, and every
    #: re-entry into ``run()`` mints a *fresh* key, which is the second billed
    #: generation the one-key rule exists to prevent. Read it before retrying
    #: anything out of ``run()``::
    #:
    #:     if getattr(exc, "resend_refused", False):
    #:         raise  # the key is spent; a retry can only bill again
    #:
    #: ``False`` everywhere else, including on a first-attempt failure that was
    #: never resent. Declared on the base, and defaulted onto the no-response
    #: failures by :func:`_stamp`, so the attribute is always readable.
    resend_refused: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.details = details
        self.request_id = request_id
        self.retry_after = retry_after


class MissingApiKey(ComfyError):
    """No credential could be resolved for a surface that requires one.

    Raised locally at client construction — before any request — when neither an
    explicit ``api_key`` argument nor ``COMFY_API_KEY`` supplied a key and the
    target is Comfy Cloud. Distinct from :class:`Unauthorized`, which is the
    server rejecting a key that *was* sent. The message names the environment
    variable, and never contains a key (there is none to contain).
    """


class Unauthorized(ComfyError):
    """The surface rejected the request for lack of a valid key.

    Comfy Cloud and serverless require a key; a self-hosted proxy needs none.
    Its local counterpart is :class:`MissingApiKey` — no key at all, caught at
    construction rather than on the wire.
    """


class Forbidden(ComfyError):
    pass


class NotFound(ComfyError):
    pass


class InvalidWorkflow(ComfyError):
    """Structural/validation failure; ``details`` carries per-node errors."""


class WorkflowFormatUi(InvalidWorkflow):
    """UI-export JSON was submitted instead of the API-format graph."""


class MissingAsset(ComfyError):
    """A ``core/ASSET`` reference was not usable (unknown/unscanned/not owned)."""


class HashMismatch(ComfyError):
    """Uploaded bytes did not match the declared ``expected_hash``."""


class BlobNotFound(ComfyError):
    """from-hash / existence probe found no blob the caller can mint from."""


class IdempotencyKeyReuse(ComfyError):
    """The idempotency key was reused. Keys are single-use (reject-on-duplicate,
    no replay): any second request with the same key — a retry, a concurrent
    duplicate, or the same key with a different body — is rejected."""


class InsufficientCredits(ComfyError):
    pass


class QueueFull(ComfyError):
    """Backpressure: the queue is full. ``retry_after`` is seconds to wait."""

    def __init__(self, message: str, *, retry_after: int, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class JobFailed(ComfyError):
    """A job reached a non-success terminal state.

    ``error`` carries the node-level detail (``.code``, ``.node_id``,
    ``.message``, ``.traceback``) when the platform provided one.
    """

    def __init__(self, message: str, *, error: JobError | None = None) -> None:
        super().__init__(message, code=(error.code if error else "job_failed"))
        self.error = error


_BY_CODE: dict[str, type[ComfyError]] = {
    "invalid_workflow": InvalidWorkflow,
    "workflow_format_ui": WorkflowFormatUi,
    "missing_asset": MissingAsset,
    "hash_mismatch": HashMismatch,
    "blob_not_found": BlobNotFound,
    "idempotency_key_reuse": IdempotencyKeyReuse,
    "insufficient_credits": InsufficientCredits,
    "not_found": NotFound,
    # public-api currently returns entity-specific 404 codes even though the
    # spec documents the generic `not_found`; map them so a missing job/asset
    # still raises the typed NotFound. (Server/spec reconciliation of the code
    # set is a separate follow-up.)
    "job_not_found": NotFound,
    "asset_not_found": NotFound,
    "unauthorized": Unauthorized,
    "forbidden": Forbidden,
}


def _router_only_class(code: str) -> type[Any] | None:
    """The typed RouterError subclass for a code only Router produces, or None.

    The low layer now preserves Router's wire bucket as the ``code`` for every
    status, so this is what turns a preserved ``not_enabled`` into the
    :class:`~comfy_sdk.router_exceptions.NotEnabled` a pre-launch caller
    catches, instead of a bare ``ComfyError``.

    Deliberately scoped to buckets the v2 envelope does NOT also use:
    ``unauthorized``, ``forbidden`` and ``insufficient_credits`` are spelled
    identically by both surfaces, and the code string alone cannot say which
    answered — retyping those would break every jobs-surface ``except
    Forbidden`` handler to fix none, since their v2 classes fire on the router
    surface too. Imported lazily because :mod:`comfy_sdk.router_exceptions`
    subclasses :class:`ComfyError` from this module.
    """
    from comfy_sdk.router_exceptions import _BY_ERROR_TYPE as _ROUTER_BY_TYPE

    if code in _BY_CODE or code == "queue_full":
        return None
    return _ROUTER_BY_TYPE.get(code)


def to_sdk_error(exc: ApiError) -> ComfyError:
    """Translate a protocol ``ApiError`` into the idiomatic SDK exception."""
    # `str(exc)`, not `exc.message`: they differ only when the protocol error
    # carries a body excerpt — a response that stated no message of its own —
    # and then `str(exc)` is the one that names the cause (`HTTP 503: no healthy
    # upstream`). Callers read the SDK exception, never the protocol one, so the
    # cause has to cross this boundary or it reaches no log.
    if exc.code == "queue_full":
        return QueueFull(
            str(exc),
            retry_after=exc.retry_after or 0,
            code=exc.code,
            http_status=exc.http_status,
            details=exc.details,
            request_id=exc.request_id,
        )
    router_cls = _router_only_class(exc.code)
    if router_cls is None and exc.validation_errors:
        # The body carried Router's `detail[]` ARRAY, which only Router sends, so
        # it identifies the Router surface even when the bucket collides with a v2
        # envelope code (`unauthorized`/`forbidden`/`insufficient_credits`, spelled
        # identically by both) or is the status-derived `invalid_workflow` guess a
        # 422 whose `X-Comfy-Error-Type` was stripped falls to. Selecting a
        # RouterError keeps both the typed entries and the summary; the plain
        # `_BY_CODE` branch below drops `.errors`, and when the body ALSO carried
        # `error.message` the summary was lost with them — `message` was already
        # set, so `error_from_envelope`'s `if not message:` summary block never
        # ran — leaving the per-field data reachable only through `__cause__`,
        # against what the `RouterError.errors` docstring promises.
        from comfy_sdk.router_exceptions import exception_for

        router_cls = exception_for(exc.code)
    if router_cls is not None:
        # Imported here for the same reason `_router_only_class` imports
        # `_BY_ERROR_TYPE` lazily: `router_exceptions` subclasses `ComfyError`
        # from this module. This is the conversion the layering rule forces —
        # `comfy_low` carries a Router validation body's `detail[]` entries up
        # raw because it may not import `comfy_sdk`, and this is the boundary
        # that can type them. Without it `.errors` was empty on the whole
        # `models.run` path while the documented contract says it is populated
        # whenever the response carried the array.
        from comfy_sdk.router_exceptions import _detail_from

        return router_cls(
            str(exc),
            error_type=exc.code,
            http_status=exc.http_status,
            request_id=exc.request_id,
            retry_after=exc.retry_after,
            errors=tuple(_detail_from(entry) for entry in exc.validation_errors),
        )
    cls = _BY_CODE.get(exc.code, ComfyError)
    # No `errors=` here, deliberately: `.errors` is a `RouterError` attribute and
    # none of these classes takes the argument. Nothing typed is lost by that: a
    # body that carried Router's `detail[]` array was already routed to a
    # RouterError above (the `exc.validation_errors` guard), so this branch is
    # reached only when the response carried no per-field entries at all — a v2
    # envelope, an intermediary's reject, or a `detail[]` whose members were none
    # of them mappings — and there is nothing to forward.
    return cls(
        str(exc),
        code=exc.code,
        http_status=exc.http_status,
        details=exc.details,
        request_id=exc.request_id,
        # Forwarded for every code, not just the throttled ones: `Retry-After`
        # is how a `deadline_exceeded` 504 paces the same-key replay that
        # collects an already-billed generation, and dropping it here left the
        # caller told to wait with nothing to wait on.
        retry_after=exc.retry_after,
    )


#: What an ``idempotency_key=`` gets stamped onto. Deliberately not
#: "everything": these are the failures of the *call* — the SDK's own errors
#: (which includes every ``RouterError``) and an httpx failure that means the
#: request did not complete. Anything else leaving the block is a bug in the
#: SDK or the caller's own code, where a key is noise, and a ``KeyboardInterrupt``
#: must not be touched at all.
#:
#: ``asyncio.CancelledError`` is the one ``BaseException`` here, and it earns
#: the place: cancelling an in-flight ``AsyncModels.run`` — which is what the
#: ``asyncio.wait_for`` a caller wraps a ten-minute call in does — abandons a
#: generation that may already be dispatched and billed, and that is precisely
#: the case the key exists to collect. It is re-raised bare like the rest of
#: this branch, so nothing is swallowed and the cancellation still propagates.
_STAMPABLE: tuple[type[BaseException], ...] = (
    ComfyError,
    httpx.HTTPError,
    asyncio.CancelledError,
)

#: Attributes a stamped exception is guaranteed to answer to, defaulted to
#: ``None`` on the ones that do not declare them. Kept beside
#: :data:`_STAMPABLE` so an attribute added to :class:`ComfyError` for the
#: caller to read inside an ``except`` block is added here too —
#: ``tests/test_error_contract.py`` pins the pairing.
_STAMPED_ATTRIBUTES = ("request_id", "retry_after")

#: Stamped like :data:`_STAMPED_ATTRIBUTES`, but defaulted to ``False``
#: rather than ``None``: these are booleans a caller tests directly, and a
#: ``None`` default would read as falsey by luck rather than by contract.
_STAMPED_FLAGS = ("resend_refused",)

_E = TypeVar("_E", bound=BaseException)


def _stamp(exc: _E, idempotency_key: str | None) -> _E:
    """Attach ``idempotency_key`` to ``exc`` in place and hand it back.

    ``setattr`` rather than a constructor argument because the transport-level
    members of :data:`_STAMPABLE` are httpx's classes, which this SDK does not
    build. A ``None`` key writes nothing, so an operation that sends no key
    leaves ``ComfyError.idempotency_key`` at its class default.

    The rest of :data:`_STAMPED_ATTRIBUTES` is defaulted alongside it, for the
    same reason and onto exactly the same exceptions: :class:`ComfyError`
    declares them on the class, but ``httpx.ConnectError`` and
    ``asyncio.CancelledError`` do not — so without this the documented surface
    would be uniform on everything *except* the no-response failures it is most
    needed on, where ``exc.request_id`` would raise ``AttributeError`` instead
    of reading ``None``. Never overwritten: a stamped exception that already
    carries one of them keeps its own value.
    """
    if idempotency_key is None:
        return exc
    exc.idempotency_key = idempotency_key  # type: ignore[attr-defined]
    for name in _STAMPED_ATTRIBUTES:
        if not hasattr(exc, name):
            setattr(exc, name, None)
    for name in _STAMPED_FLAGS:
        if not hasattr(exc, name):
            setattr(exc, name, False)
    return exc


@contextmanager
def translating(*, idempotency_key: str | None = None) -> Iterator[None]:
    """Re-raise any protocol ``ApiError`` as its idiomatic SDK exception.

    Wrap the SDK-level operations that call ``comfy_low`` with this so integrators
    only ever catch ``comfy_sdk`` exceptions (``MissingAsset``, ``HashMismatch``,
    ``NotFound``, ...), never the raw protocol error.

    ``idempotency_key`` is the key the wrapped call was made under. Give it and
    every failure that leaves this block carries it as ``.idempotency_key`` —
    the one place that can, because the key is a local of the caller's frame and
    is otherwise lost the moment the exception propagates past it. That matters
    on :meth:`comfy_sdk.models.Models.run`, where the router's replay contract
    lets a caller who lost the response collect the generation they were already
    billed for by resending under the *same* key. Stamping here rather than in
    each exception's constructor is what makes an unrecognised ``error_type``,
    which falls through to the base ``RouterError``, carry it too. Omit it and
    this behaves exactly as it did before the parameter existed.
    """
    try:
        yield
    except ApiError as exc:
        raise _stamp(to_sdk_error(exc), idempotency_key) from exc
    except _STAMPABLE as exc:
        # Already on a surface a caller catches — a RouterError the transport
        # raised directly, an httpx failure with no response to translate, or a
        # cancellation of a call that may already be dispatched. Nothing to
        # convert, but the key still has to ride out with it. A bare `raise`
        # keeps the original traceback and the cancellation semantics, so with
        # no key given this branch is indistinguishable from not catching at
        # all.
        _stamp(exc, idempotency_key)
        raise
