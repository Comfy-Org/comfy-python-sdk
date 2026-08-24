"""Typed exceptions for the Comfy Router model-run surface (``client.models``).

One class per ``error_type`` in the router's closed error set, so a caller
branches on a class rather than on a string::

    from comfy_sdk.router_exceptions import ContentPolicyViolation, RouterError

    try:
        ...                                    # a client.models call
    except ContentPolicyViolation as exc:
        print(exc.detail, exc.request_id)      # deterministic — do not retry
    except RouterError as exc:
        report(exc.error_type, exc.request_id)

Three properties of this module are contract, not taste:

**The class name is the PascalCase of the wire value, always.** ``invalid_input``
is :class:`InvalidInput`, ``content_policy_violation`` is
:class:`ContentPolicyViolation`. That mechanical rule is what keeps this list and
the TypeScript SDK's identical without either side maintaining a second table --
an example ported between the two languages finds the same names.
``test_router_exceptions.py`` asserts the rule over every class, so a
hand-picked name cannot creep in.

**Every class derives from :class:`RouterError`.** A caller who wants the broad
catch writes ``except RouterError``; one who wants a single failure mode names
it. ``RouterError`` in turn derives from :class:`~comfy_sdk.exceptions.ComfyError`,
so ``except ComfyError`` still covers the whole SDK.

**An unrecognised ``error_type`` raises the base class rather than failing.**
The error set grows on the server's release cycle while an SDK is pinned by its
users, so a bucket this version has never heard of must still arrive as a
catchable exception carrying its raw ``error_type`` -- treat one like
``internal_error``. A client that rejected the unknown value would fail hardest
exactly when something has already gone wrong.

Three of these names -- ``Unauthorized``, ``Forbidden``, ``InsufficientCredits``
-- already exist in :mod:`comfy_sdk.exceptions` for the workflow surface, where
they mean the same thing about a different API. They are deliberately *not*
merged and this module is deliberately *not* re-exported from the package root:
one name cannot be two classes, and silently making ``comfy_sdk.Unauthorized``
mean the router one would change what an existing ``except`` clause catches.
Import the router ones from this module, or catch
:class:`~comfy_sdk.exceptions.ComfyError` to cover both surfaces at once.

The coarse bucket is not the whole story. A per-field model-validation failure
carries a ``detail`` *array* whose entries keep the specific, provider-level
reason; those arrive as :class:`ValidationErrorDetail` on ``.errors``, with
``loc``/``msg``/``type``/``ctx`` readable as data. The exception message
summarises them for a human, but the branch a caller writes reads the entries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .exceptions import ComfyError

#: Response header carrying the coarse failure bucket. Set on every router error
#: response, and on a per-field validation failure it is the only place the
#: bucket appears -- that body has no ``error_type`` field of its own.
ERROR_TYPE_HEADER = "X-Comfy-Error-Type"

#: Response header carrying the server-minted id for the call, present on every
#: response. It is the id a user quotes in a support request, so it is attached
#: to every exception this module raises.
REQUEST_ID_HEADER = "X-Comfy-Request-Id"


@dataclass(frozen=True)
class ValidationErrorDetail:
    """One per-field model-validation failure.

    ``type`` is the specific, provider-level reason (``value_error``,
    ``missing``, ``image_too_small``, ``file_too_large``, ...) and is where the
    granularity the coarse ``error_type`` bucket cannot express survives. It is
    an open string: the provider vocabulary grows on the provider's release
    cycle, not the SDK's, so an unmodelled value must reach the caller rather
    than fail decoding.
    """

    #: Path to the offending field, outermost segment first --
    #: ``("body", "image_url")``, or ``("body", "images", 0)`` where an integer
    #: indexes into an array.
    loc: tuple[str | int, ...] = ()
    #: Human-readable description of this single failure.
    msg: str = ""
    #: Specific, machine-readable reason, passed through from the provider.
    type: str = ""
    #: The violated bound, when the reason carries one -- ``{"limit_value": 8}``
    #: alongside ``greater_than``, ``{"min_width": 512}`` alongside
    #: ``image_too_small``. Deliberately an open mapping: narrowing it to a fixed
    #: field list is how a ported integration silently loses the branch that read
    #: the bound. ``None`` when the reason carries no bound.
    ctx: Mapping[str, Any] | None = None
    #: The offending input value echoed back, so a caller can see what was
    #: rejected without re-deriving it from ``loc``. Any JSON type, and ``None``
    #: both when the value was null and when it was not echoed back at all.
    input: Any = None

    @property
    def location(self) -> str:
        """``loc`` as a dotted path -- ``body.images.0`` -- for display."""
        return ".".join(str(part) for part in self.loc)


class RouterError(ComfyError):
    """Base for every Comfy Router failure, and what an unknown bucket raises.

    ``error_type`` is the wire value; on this base class it is whatever the
    server sent, including a value this SDK version does not know.
    """

    #: The wire ``error_type`` this class maps to. Empty on the base, which is
    #: reached only by an unrecognised bucket (the instance sets it there).
    error_type: str = ""

    def __init__(
        self,
        detail: str,
        *,
        error_type: str | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
        errors: Sequence[ValidationErrorDetail] = (),
    ) -> None:
        resolved = error_type if error_type is not None else self.error_type
        super().__init__(detail, code=resolved or None, http_status=http_status)
        if error_type is not None:
            self.error_type = error_type
        #: Human-readable description of the failure, safe to show a user. Not
        #: machine-parsed -- branch on the exception class instead.
        self.detail = detail
        #: Server-minted id for the call, from ``X-Comfy-Request-Id``. ``None``
        #: only when the response carried no such header.
        self.request_id = request_id
        #: Per-field validation failures, populated whenever the response
        #: carried the ``detail`` *array* -- see :class:`ValidationErrorDetail`.
        #: Empty for every failure that names no field. It lives on the base
        #: rather than on one subclass because the bucket a per-field failure
        #: carries is read off the header, so the entries have to survive
        #: whichever bucket that turns out to be.
        self.errors: tuple[ValidationErrorDetail, ...] = tuple(errors)


# -- the six request-level buckets -------------------------------------------


class InvalidInput(RouterError):
    """The request was rejected as invalid for this model."""

    error_type = "invalid_input"


class ContentPolicyViolation(RouterError):
    """The model's content policy refused the request.

    Deterministic: the same input will be refused again, so this is never a
    retry candidate. It is deliberately not a :class:`ProviderError` -- the two
    differ in whether a retry can ever succeed.
    """

    error_type = "content_policy_violation"


class ProviderError(RouterError):
    """The upstream model provider returned an error."""

    error_type = "provider_error"


class ProviderTimeout(RouterError):
    """The upstream model provider did not respond in time.

    An upstream running out of time, not our own deadline -- the two are
    separate buckets because they are separate causes.
    """

    error_type = "provider_timeout"


class InsufficientCredits(RouterError):
    """The account does not have enough credits to run this model."""

    error_type = "insufficient_credits"


class ModelNotFound(RouterError):
    """No such model. An unknown provider lands here too: both are "that id
    names nothing"."""

    error_type = "model_not_found"


# -- the five transport-level buckets ----------------------------------------


class Unauthorized(RouterError):
    """Authentication is required, or the key presented was not accepted."""

    error_type = "unauthorized"


class Forbidden(RouterError):
    """The caller is authenticated but has no access to this model."""

    error_type = "forbidden"


class ConcurrencyLimitExceeded(RouterError):
    """Too many concurrent requests."""

    error_type = "concurrency_limit_exceeded"


class ClientDisconnected(RouterError):
    """The client closed the connection before the request completed."""

    error_type = "client_disconnected"


class InternalError(RouterError):
    """An internal error occurred."""

    error_type = "internal_error"


#: Every class in the closed set, in the order the error set declares it: the
#: six request-level buckets, then the five transport-level ones.
ROUTER_EXCEPTIONS: tuple[type[RouterError], ...] = (
    InvalidInput,
    ContentPolicyViolation,
    ProviderError,
    ProviderTimeout,
    InsufficientCredits,
    ModelNotFound,
    Unauthorized,
    Forbidden,
    ConcurrencyLimitExceeded,
    ClientDisconnected,
    InternalError,
)

_BY_ERROR_TYPE: dict[str, type[RouterError]] = {cls.error_type: cls for cls in ROUTER_EXCEPTIONS}

#: The closed ``error_type`` set this SDK version knows, in declaration order.
#: Anything outside it raises :class:`RouterError` itself.
ROUTER_ERROR_TYPES: tuple[str, ...] = tuple(_BY_ERROR_TYPE)

# Status -> bucket, used ONLY when a response carries no bucket at all: a proxy,
# gateway or load balancer that rejects a call before it reaches the router
# answers with a status and no `X-Comfy-Error-Type`, and `except Unauthorized`
# should still fire for a rejected key.
#
# Only statuses the error contract assigns to exactly ONE bucket appear here.
# `400` is deliberately absent because it carries either `invalid_input` or
# `content_policy_violation`, and those differ in whether a retry can ever
# succeed -- guessing between them would tell a caller to retry a deterministic
# refusal. `422` is absent for the same reason: the contract pins no bucket to
# it. Both fall through to `RouterError`, which is the honest answer.
_ERROR_TYPE_BY_STATUS: dict[int, str] = {
    401: "unauthorized",
    402: "insufficient_credits",
    403: "forbidden",
    404: "model_not_found",
    429: "concurrency_limit_exceeded",
    499: "client_disconnected",
    502: "provider_error",
    504: "provider_timeout",
}


def exception_for(error_type: str | None) -> type[RouterError]:
    """The exception class for a wire ``error_type``.

    Returns :class:`RouterError` for a value this SDK version does not know,
    which is the forward-compatible answer: the caller still catches it and can
    read the raw value off ``.error_type``.
    """
    if error_type is None:
        return RouterError
    return _BY_ERROR_TYPE.get(error_type, RouterError)


def error_from_response(
    http_status: int,
    headers: Mapping[str, str] | None = None,
    body: Any = None,
) -> RouterError:
    """Build the typed exception for a router error response.

    ``body`` is the decoded JSON body, or ``None`` when there wasn't one (an
    HTML error page from an intermediary, an empty 502). The bucket is read from
    ``X-Comfy-Error-Type`` first and from the body's ``error_type`` second,
    because the per-field validation body carries no ``error_type`` of its own.

    This never raises. A malformed or unrecognised body degrades to the most
    specific exception the response still supports -- worst case a bare
    :class:`RouterError` -- because a client that blew up decoding an error
    response would replace a diagnosable failure with an undiagnosable one.
    """
    lowered = _lowercase_headers(headers)
    request_id = _clean(lowered.get(REQUEST_ID_HEADER.lower()))
    error_type = _clean(lowered.get(ERROR_TYPE_HEADER.lower()))

    detail: str | None = None
    errors: tuple[ValidationErrorDetail, ...] = ()
    if isinstance(body, Mapping):
        raw_detail = body.get("detail")
        if isinstance(raw_detail, str):
            detail = raw_detail or None
        elif isinstance(raw_detail, Sequence) and not isinstance(raw_detail, (str, bytes)):
            errors = tuple(
                _detail_from(entry) for entry in raw_detail if isinstance(entry, Mapping)
            )
        if error_type is None:
            error_type = _clean(body.get("error_type"))

    if error_type is None:
        error_type = _ERROR_TYPE_BY_STATUS.get(http_status)

    if detail is None:
        detail = _summarise(errors) or f"HTTP {http_status}"

    return exception_for(error_type)(
        detail,
        error_type=error_type,
        http_status=http_status,
        request_id=request_id,
        errors=errors,
    )


def _lowercase_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Header names are case-insensitive on the wire; normalise so a plain dict
    works as well as an ``httpx.Headers``."""
    if not headers:
        return {}
    return {str(name).lower(): value for name, value in headers.items()}


def _clean(value: Any) -> str | None:
    """A header or body field as a non-empty string, or ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _detail_from(entry: Mapping[str, Any]) -> ValidationErrorDetail:
    """One ``detail[]`` entry, tolerating a missing or wrongly-typed field.

    Every field is optional here even though the contract makes ``loc``, ``msg``
    and ``type`` required: this runs while handling a failure, and dropping the
    two fields that did arrive because a third was malformed helps nobody.
    """
    raw_loc = entry.get("loc")
    loc: tuple[str | int, ...] = ()
    if isinstance(raw_loc, Sequence) and not isinstance(raw_loc, (str, bytes)):
        loc = tuple(part if isinstance(part, (str, int)) else str(part) for part in raw_loc)

    msg, reason, ctx = entry.get("msg"), entry.get("type"), entry.get("ctx")
    return ValidationErrorDetail(
        loc=loc,
        msg=msg if isinstance(msg, str) else "",
        type=reason if isinstance(reason, str) else "",
        ctx=ctx if isinstance(ctx, Mapping) else None,
        input=entry.get("input"),
    )


def _summarise(errors: Sequence[ValidationErrorDetail]) -> str:
    """A one-line message for a per-field failure.

    This is *in addition to* ``.errors``, never instead of it -- the entries stay
    readable as data, and a caller branching on a field reads them rather than
    parsing this back apart.
    """
    parts: list[str] = []
    for entry in errors:
        if entry.location and entry.msg:
            parts.append(f"{entry.location}: {entry.msg}")
        elif entry.location or entry.msg:
            parts.append(entry.location or entry.msg)
    return "; ".join(parts)


__all__ = [
    "ERROR_TYPE_HEADER",
    "REQUEST_ID_HEADER",
    "ROUTER_ERROR_TYPES",
    "ROUTER_EXCEPTIONS",
    "ClientDisconnected",
    "ConcurrencyLimitExceeded",
    "ContentPolicyViolation",
    "Forbidden",
    "InsufficientCredits",
    "InternalError",
    "InvalidInput",
    "ModelNotFound",
    "ProviderError",
    "ProviderTimeout",
    "RouterError",
    "Unauthorized",
    "ValidationErrorDetail",
    "error_from_response",
    "exception_for",
]
