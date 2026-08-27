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

**The set comes from the vendored contract, not from this file.**
``spec/router-openapi.yaml`` carries the closed bucket list as
``x-comfy-error-types``, one entry per wire value with the ``meaning`` text the
class docstrings below reproduce. ``tests/test_router_spec_contract.py`` reads
that list and asserts a class exists for every value, in the same order, so a
bucket the contract adds cannot land in the SDK as an untyped
``RouterError`` without a test going red. ``scripts/check_drift.py`` runs the
same comparison in CI, which is what makes the next vendored Router sync a real
diff review rather than a silent widening.

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

That holds for the buckets whose names collide with *nothing* too --
:class:`NotEnabled`, :class:`ServiceUnavailable`, :class:`RateLimited`,
:class:`DeadlineExceeded`. Lifting the non-colliding subset to the package root
would make ``comfy_sdk.NotEnabled`` importable while ``comfy_sdk.InvalidInput``
stayed a name that does not exist, and a caller cannot be expected to remember
which half of one hierarchy lives where. One import path for the whole set is
the property worth keeping; ``from comfy_sdk.router_exceptions import
NotEnabled`` is it.

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

from comfy_low.errors import clean_request_id

from .exceptions import ComfyError

#: Response header carrying the coarse failure bucket. Set on every router error
#: response, and on a per-field validation failure it is the only place the
#: bucket appears -- that body has no ``error_type`` field of its own.
ERROR_TYPE_HEADER = "X-Comfy-Error-Type"

#: Response header carrying the server-minted id for the call, present on every
#: response. It is the id a user quotes in a support request, so it is attached
#: to every exception this module raises.
REQUEST_ID_HEADER = "X-Comfy-Request-Id"

#: Response header naming the pace at which to ask again, in seconds. The
#: contract documents it on the throttled buckets (:class:`RateLimited`,
#: :class:`ConcurrencyLimitExceeded`) and on a ``deadline_exceeded`` ``504``.
#: It is read here rather than dropped because
#: :func:`comfy_sdk.retry.retry_after_of` is what
#: :meth:`~comfy_sdk.retry.RetryPolicy.should_retry` keys both of its
#: default-on branches on -- the ``429`` and the collectable ``504``/``409``:
#: a router error that arrived without this attribute would make those branches
#: unreachable and turn the retry the default policy is built to make into a
#: silent no-op.
RETRY_AFTER_HEADER = "Retry-After"


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
        retry_after: int | None = None,
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
        #: Seconds the server asked the caller to wait, from ``Retry-After``,
        #: or ``None`` when it named no pace. Named identically to
        #: ``comfy_low.ApiError.retry_after`` on purpose: the retry policy
        #: reads it off either with one ``getattr``, so a router error and a
        #: protocol one are paced the same way.
        self.retry_after = retry_after
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


# -- the nine transport-level buckets ----------------------------------------


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
    """An internal error occurred.

    It is also the value a client should treat any *unrecognised* bucket as, so
    a later addition to the set does not break a client generated before it.
    """

    error_type = "internal_error"


class DeadlineExceeded(RouterError):
    """Comfy stopped holding the connection at its own configured bound before
    an answer arrived.

    It shares ``504`` with :class:`ProviderTimeout` and the pair says which side
    ran out of time; this one is Comfy's own bound, so nothing about the request
    was rejected and the same request may be retried. It says nothing about the
    charge: a provider generation that completed is billed regardless of whether
    the caller received the response. Retry it with the *same*
    ``Idempotency-Key`` -- when the provider had already accepted the
    generation, the retry collects that generation rather than dispatching
    another, and a ``Retry-After`` on the ``504`` says when to ask.

    That is the *contract's* advice, and unlike :class:`ServiceUnavailable` the
    SDK **does** follow it by default -- this is the one bucket a contract says
    the same key survives, so ``client.models.run`` makes the retry rather than
    describing it. :func:`comfy_sdk.retry.is_collectable` is the gate and it
    keys on this bucket, never on the status alone: a ``504`` reaching the SDK
    without a bucket header is read as :class:`ProviderTimeout`, where a
    same-key retry is *not* blessed. It also requires the ``Retry-After``, which
    the router sends only when it holds a handle to a generation still running
    -- without one there is nothing to collect, so a bare
    ``deadline_exceeded`` ``504`` is raised to the caller like any other 5xx.
    The pace is honoured as given: ``error_from_response`` preserves the header
    on the exception, and
    :meth:`~comfy_sdk.retry.Retrier.delay_before_retry` prefers a named pace
    over its own jittered backoff. ``RetryPolicy(retry_collectable=False)``
    switches the behaviour off; ``Comfy(retry=NO_RETRY)`` switches off retrying
    entirely.
    """

    error_type = "deadline_exceeded"


class NotEnabled(RouterError):
    """Comfy Router is not switched on for this caller yet.

    Nothing about the request is wrong and the model exists, which is why this
    is not :class:`ModelNotFound`; it shares ``403`` with :class:`Forbidden` and
    is *not* the same thing, because ``forbidden`` is an entitlement decision
    about the caller while this is a state of the rollout. It is **terminal**:
    do not retry, and do not treat it as an outage.
    """

    error_type = "not_enabled"


class ServiceUnavailable(RouterError):
    """A service Comfy Router depends on is temporarily unavailable and the
    caller did nothing wrong.

    Retry it with backoff: it is the one bucket here whose condition clears on
    its own, without the caller changing the request and without a concurrency
    slot freeing, which is what distinguishes it from the other retryable
    answers (:class:`ConcurrencyLimitExceeded`, :class:`DeadlineExceeded`). It
    is separate from :class:`InternalError` -- which is a ``500`` and means
    Router itself failed -- so a client can tell "come back shortly" from "this
    call is not going to work".

    On whether the SDK makes that retry for you: it does **not** by default, and
    :mod:`comfy_sdk.retry` says why -- the bucket arrives on a ``503``, nothing
    in either vendored contract says a ``503`` *releases* the
    ``Idempotency-Key``, and the default policy retries only failures the one
    key provably survives. ``RetryPolicy(retry_possibly_in_flight=True)`` opts
    in, and is the route to take: it keeps the one key across the retry.

    Catching this class and calling ``models.run()`` again is **not** an
    equivalent way to do it. A run mints a *fresh* ``Idempotency-Key`` per call,
    so a bare re-run after a ``503`` presents a new key for a generation the
    server may already have started, and the two can both be billed. A
    hand-written retry has to pass the *same* explicit ``idempotency_key=`` both
    times, and is only useful against a deployment documented to replay a
    repeated key rather than reject it ``422``.
    """

    error_type = "service_unavailable"


class RateLimited(RouterError):
    """The caller has spent an allowance measured over a *window* and must wait
    for that window to roll.

    It shares ``429`` with :class:`ConcurrencyLimitExceeded` and is not the same
    thing: that one clears the moment one of the caller's own in-flight calls
    finishes, so retrying in seconds is right, whereas nothing the caller does
    drains this one early. ``detail`` names the window.
    """

    error_type = "rate_limited"


#: Every class in the closed set, in the order the error set declares it: the
#: six request-level buckets, then the nine transport-level ones. The order is
#: the vendored spec's ``x-comfy-error-types`` order, and
#: ``tests/test_router_spec_contract.py`` asserts that -- so this tuple cannot
#: drift from the contract two SDKs generate their surface from.
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
    DeadlineExceeded,
    NotEnabled,
    ServiceUnavailable,
    RateLimited,
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
# Read it as "what does an INTERMEDIARY's answer most likely mean", not "which
# bucket owns this status". Nothing here is ever consulted for a response the
# router itself sent, because the router repeats the bucket on the header on
# every error it sends -- so each entry is the plain HTTP reading of the status,
# and the bucket named is whichever of ours matches that reading.
#
# The widened error set gave `403`, `429` and `504` a second router bucket each
# (`not_enabled`, `rate_limited`, `deadline_exceeded`). The mappings are
# unchanged anyway, deliberately: `forbidden`, `concurrency_limit_exceeded` and
# `provider_timeout` are the ones that restate the plain HTTP meaning of a
# rejected credential, a throttled caller and an upstream that did not answer,
# while the three new buckets are Comfy-specific claims about the router's own
# rollout, allowance windows and connection bound -- claims a proxy that never
# reached the router is not in a position to be making. (For `429` the choice is
# the narrowest one: both candidates are throttling, and `RetryPolicy` keys on
# the status and the `Retry-After` this module now preserves rather than on the
# bucket, so the class a caller catches is the only thing that differs.)
#
# A status stays OUT of the table when the plain HTTP reading does not pick one
# bucket. `400` is absent because it carries either `invalid_input` or
# `content_policy_violation`, and those differ in whether a retry can ever
# succeed -- guessing between them would tell a caller to retry a deterministic
# refusal. `422` is absent because the contract pins no bucket to it at all.
# `503` is absent because an intermediary's bare `503` is not the router's
# `service_unavailable`: that bucket is the router's own statement that a
# dependency of ITS is briefly down and the condition clears on its own, and a
# gateway's `503` says nothing about whether the request ever reached the router
# to have such a statement made about it. All three fall through to
# `RouterError`, which is the honest answer.
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
    # Bounded and filtered rather than merely stripped: the id is displayed
    # and pasted into support tickets, and this header is server-controlled.
    # Shared with `comfy_low.transport` so both surfaces clean it identically.
    request_id = clean_request_id(lowered.get(REQUEST_ID_HEADER.lower()))
    error_type = _clean(lowered.get(ERROR_TYPE_HEADER.lower()))
    retry_after = _retry_after(lowered.get(RETRY_AFTER_HEADER.lower()))

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
        retry_after=retry_after,
        errors=errors,
    )


def _lowercase_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Header names are case-insensitive on the wire; normalise so a plain dict
    works as well as an ``httpx.Headers``."""
    if not headers:
        return {}
    return {str(name).lower(): value for name, value in headers.items()}


def _retry_after(value: Any) -> int | None:
    """``Retry-After`` as whole seconds, or ``None`` when it named no usable pace.

    Delta-seconds only, matching ``comfy_low.transport._retry_after`` so one
    header has one reading across the SDK: the HTTP-date form the RFC also
    permits is treated as absent rather than parsed here, since guessing at a
    clock skew is worse than falling back to the local backoff schedule. A
    negative value is absent too -- it would otherwise reach the retry
    arithmetic as a delay to be waited.
    """
    if not isinstance(value, str):
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


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
    "RETRY_AFTER_HEADER",
    "ROUTER_ERROR_TYPES",
    "ROUTER_EXCEPTIONS",
    "ClientDisconnected",
    "ConcurrencyLimitExceeded",
    "ContentPolicyViolation",
    "DeadlineExceeded",
    "Forbidden",
    "InsufficientCredits",
    "InternalError",
    "InvalidInput",
    "ModelNotFound",
    "NotEnabled",
    "ProviderError",
    "ProviderTimeout",
    "RateLimited",
    "RouterError",
    "ServiceUnavailable",
    "Unauthorized",
    "ValidationErrorDetail",
    "error_from_response",
    "exception_for",
]
