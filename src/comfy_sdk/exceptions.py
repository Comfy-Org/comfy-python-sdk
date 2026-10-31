"""Idiomatic ``comfy_sdk`` exceptions.

These wrap the protocol-level ``comfy_low.ApiError`` codes with names an
integrator catches directly (``JobFailed``, ``QueueFull``, ...). ``to_sdk_error``
maps a raised ``ApiError`` to the right subclass; anything unmapped stays a
``ComfyError`` carrying the original code.

**This module and :mod:`comfy_sdk.router_exceptions` share classes, they do not
shadow each other.** Every name the two modules both export is one class object
re-exported, never two classes wearing one name -- so
``comfy_sdk.exceptions.InsufficientCredits is
comfy_sdk.router_exceptions.InsufficientCredits``, and an
``except InsufficientCredits`` written against either import catches whatever
the other one does. ``tests/test_exception_modules.py`` enumerates both
modules' exports and fails on any shared name that is not the same object, so
this cannot drift back apart.

It did drift apart once, and the failure mode is why the rule is now a test:
the classes ``to_sdk_error`` raised for a Router refusal were this module's, and
this module's did not descend from
:class:`~comfy_sdk.router_exceptions.RouterError`. ``except RouterError`` --
the obvious catch-all, and the one a careful caller reaches for over
``except Exception`` -- therefore caught nothing at all on the buckets whose
names the two modules shared.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import httpx

from comfy_low.errors import _CANCEL_REFUSAL_STATUS, ApiError
from comfy_low.models import JobError

from ._errors import ComfyError
from .router_exceptions import (
    _BY_CANCEL_REFUSAL,
    _BY_ERROR_TYPE,
    AlreadyCompleted,
    CancelRefused,
    Forbidden,
    InsufficientCredits,
    RouterError,
    Unauthorized,
    _detail_from,
)


class MissingApiKey(ComfyError):
    """No credential could be resolved for a surface that requires one.

    Raised locally at client construction — before any request — when neither an
    explicit ``api_key`` argument nor ``COMFY_API_KEY`` supplied a key and the
    target is Comfy Cloud. Distinct from :class:`Unauthorized`, which is the
    server rejecting a key that *was* sent. The message names the environment
    variable, and never contains a key (there is none to contain).
    """


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


#: Wire ``code`` -> the class this SDK raises for it. Only the codes the v2
#: envelope owns are listed here; the Router buckets are looked up in
#: :data:`comfy_sdk.router_exceptions._BY_ERROR_TYPE` instead, so that table
#: stays the single copy of the contract's closed set.
#:
#: Three entries -- ``insufficient_credits``, ``unauthorized``, ``forbidden`` --
#: name classes that live in :mod:`comfy_sdk.router_exceptions` and descend from
#: :class:`~comfy_sdk.router_exceptions.RouterError`. That is deliberate and it
#: is the fix: both surfaces spell those buckets identically, the wire ``code``
#: alone cannot say which surface answered, and raising a class that is *not* a
#: ``RouterError`` for a Router refusal is what made ``except RouterError`` a
#: dead handler. One class per bucket, reachable from both modules, is the only
#: shape where neither ``except`` clause is wrong.
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


def _class_for(exc: ApiError) -> type[ComfyError]:
    """The class ``exc`` becomes, from its wire ``code`` and its surface.

    Three lookups in precedence order, then a fallback that depends on which
    surface answered:

    1. :data:`_BY_CODE` -- the v2 envelope's codes, plus the three buckets both
       surfaces spell the same way.
    2. :data:`~comfy_sdk.router_exceptions._BY_ERROR_TYPE` -- the Router
       contract's closed set. The low layer preserves Router's bucket as the
       ``code`` for every status, so this is what turns a preserved
       ``not_enabled`` into the :class:`~comfy_sdk.router_exceptions.NotEnabled`
       a pre-launch caller catches.
    3. :data:`~comfy_sdk.router_exceptions._BY_CANCEL_REFUSAL` -- the cancel
       route's refusals, which the contract's bucket list does not name because
       they are not run-route buckets. Consulted only for a ``409``: the lookup
       is on ``code``, and ``code`` is also whatever a v2 envelope's
       ``error.code`` said, on any route at any status, so without the status
       gate an unrelated failure that happened to spell ``already_completed``
       inherited :class:`~comfy_sdk.router_exceptions.AlreadyCompleted` and the
       benign "still collectable" reading that class documents.

    The fallback is where the *surface* matters.
    :attr:`comfy_low.errors.ApiError.error_type` is set only when the response
    identified itself as Router's, so a bucket added to Router after this SDK
    version was built still reaches the caller as a
    :class:`~comfy_sdk.router_exceptions.RouterError` -- the forward-compatible
    answer :func:`~comfy_sdk.router_exceptions.exception_for` already gives on
    the queued surface, and without it ``except RouterError`` would still have a
    hole on exactly the refusals nobody could have enumerated in advance.
    Anything else stays a bare ``ComfyError`` carrying the original code.
    """
    cls = _BY_CODE.get(exc.code) or _BY_ERROR_TYPE.get(exc.code)
    if cls is None and exc.http_status == _CANCEL_REFUSAL_STATUS:
        # Gated on the STATUS as well as the code, which the low layer already
        # does for the body-shape half (`_cancel_refusal_code` returns None off
        # `409`). `_BY_CANCEL_REFUSAL` names refusals the CANCEL route answers
        # `409` with and nothing else, but the lookup is on `exc.code`, and
        # `code` is also whatever an envelope's `error.code` said — on any route
        # and any status. Without this gate a `200`-adjacent `4xx` from a job or
        # asset route whose envelope happened to say `already_completed` became
        # `AlreadyCompleted`, which this SDK documents as benign and "the result
        # is still collectable". That is a promise those routes never made.
        cls = _BY_CANCEL_REFUSAL.get(exc.code)
    if cls is not None:
        return cls
    return RouterError if exc.error_type is not None else ComfyError


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
    cls = _class_for(exc)
    if issubclass(cls, RouterError):
        # `RouterError` spells the bucket `error_type` rather than `code` (it
        # sets `code` from it), and takes `detail` positionally as the
        # human-readable string. `details` — the per-field dict the v2 envelope
        # carries — is forwarded too: nothing about a shared bucket says the
        # response cannot have sent one.
        #
        # `errors=` is the conversion the layering rule forces: `comfy_low`
        # carries a Router validation body's `detail[]` entries up raw because
        # it may not import `comfy_sdk`, and this is the boundary that can type
        # them. Without it `.errors` was empty on the whole `models.run` path
        # while the documented contract says it is populated whenever the
        # response carried the array. `_detail_from` is a plain module-level
        # import now that `ComfyError` lives in `comfy_sdk._errors` — the cycle
        # that forced the lazy one is what this change removed.
        return cls(
            str(exc),
            error_type=exc.code,
            http_status=exc.http_status,
            details=exc.details,
            request_id=exc.request_id,
            retry_after=exc.retry_after,
            errors=tuple(_detail_from(entry) for entry in exc.validation_errors),
            refusal_subject=exc.refusal_subject,
        )
    # No `errors=` below, deliberately: `.errors` is a `RouterError` attribute
    # and none of the remaining classes takes the argument. A validation body
    # that reaches this branch — a `detail[]` under a v2 `error.code`, or under
    # the status-derived guess when no bucket was sent at all — still gets the
    # entries' summary, since `summarise_detail` made it `exc.message` one layer
    # down. The same holds for the `queue_full` early return above.
    #
    # The array is deliberately NOT used to reroute these into the Router
    # hierarchy. `detail[]` is a body shape any server, proxy or gateway can
    # send (a FastAPI `RequestValidationError` is exactly it), so keying the
    # class off it would let an intermediary in front of the v2 jobs surface
    # decide which `except` a caller runs. Provenance is the header, and
    # `_class_for` already reads it: `error_type` is set only for a response
    # that identified itself as Router's, and for those this branch is
    # unreachable — every Router bucket resolves to a `RouterError` subclass,
    # including the three both surfaces spell alike, so the entries are
    # forwarded above.
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


#: Explicit because several of these names are re-exports rather than
#: definitions: :class:`~comfy_sdk.router_exceptions.RouterError` and the three
#: buckets both surfaces spell the same way are defined in
#: :mod:`comfy_sdk.router_exceptions`, and ``ComfyError`` in
#: :mod:`comfy_sdk._errors` -- one class object each, so a name this module and
#: that one share is the *same* class and either import catches what the other
#: does. ``tests/test_exception_modules.py`` reads this list to assert it.
__all__ = [
    "AlreadyCompleted",
    "BlobNotFound",
    "CancelRefused",
    "ComfyError",
    "Forbidden",
    "HashMismatch",
    "IdempotencyKeyReuse",
    "InsufficientCredits",
    "InvalidWorkflow",
    "JobFailed",
    "MissingApiKey",
    "MissingAsset",
    "NotFound",
    "QueueFull",
    "RouterError",
    "Unauthorized",
    "WorkflowFormatUi",
    "to_sdk_error",
    "translating",
]
