"""The shared error envelope mapped to a typed exception per ``code``.

This is the ``comfy_low`` (protocol) view of errors: one class per documented
error ``code``, plus a fallback. ``comfy_sdk`` re-raises these as its own
idiomatic exceptions where it adds value (e.g. ``JobFailed`` carrying node
details), but the protocol codes are defined here so the generated layer has a
stable, typed error surface.
"""

from __future__ import annotations

import re
from typing import Any

#: The leading run of characters a request id may consist of, bounded in the
#: pattern itself. ``X-Comfy-Request-Id`` is server-controlled and the id is
#: meant to be *displayed* — rendered in a traceback, written to a log, pasted
#: into a support ticket — so it is reduced to something safe to display rather
#: than kept verbatim. Matching a leading run (rather than deleting the
#: offending bytes) also gives the right answer for the one case a well-behaved
#: server can still produce: ``httpx.Headers.get`` joins duplicate headers with
#: ``", "``, and a comma is not in the class, so ``"a1, a2"`` yields ``"a1"``
#: instead of a spliced ``"a1a2"`` that identifies no call at all.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:+/=@-]{1,200}")


def clean_request_id(raw: Any) -> str | None:
    """``raw`` reduced to a bounded, printable request id, or ``None``.

    Defined here rather than beside either reader because both error surfaces
    parse the same header off their own response — ``comfy_low.transport`` off
    the shared envelope, ``comfy_sdk.router_exceptions`` off the router's — and
    an id that is safe to display on one of them has to be safe on the other.
    """
    if not isinstance(raw, str):
        return None
    match = _REQUEST_ID_RE.match(raw.strip())
    return match.group(0) if match else None


class ApiError(Exception):
    """Base for every error carried by the API's error envelope."""

    code: str = "error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.http_status = http_status
        self.details = details
        self.retry_after = retry_after
        #: Server-minted id for the call, read off ``X-Comfy-Request-Id``.
        #: ``None`` when the response carried no such header. Surfaced the same
        #: way ``retry_after`` is — a response header kept on the exception,
        #: because it is the id a user quotes in a support request and it is
        #: unreachable once the response object is gone.
        self.request_id = request_id


class InvalidWorkflow(ApiError):
    code = "invalid_workflow"


class WorkflowFormatUi(ApiError):
    code = "workflow_format_ui"


class MissingAsset(ApiError):
    code = "missing_asset"


class HashMismatch(ApiError):
    code = "hash_mismatch"


class BlobNotFound(ApiError):
    code = "blob_not_found"


class IdempotencyKeyReuse(ApiError):
    code = "idempotency_key_reuse"


class QueueFull(ApiError):
    code = "queue_full"


class InsufficientCredits(ApiError):
    code = "insufficient_credits"


class NotFound(ApiError):
    code = "not_found"


class Unauthorized(ApiError):
    code = "unauthorized"


class Forbidden(ApiError):
    code = "forbidden"


# code -> exception class. Anything unmapped becomes a bare ApiError.
_BY_CODE: dict[str, type[ApiError]] = {
    cls.code: cls
    for cls in (
        InvalidWorkflow,
        WorkflowFormatUi,
        MissingAsset,
        HashMismatch,
        BlobNotFound,
        IdempotencyKeyReuse,
        QueueFull,
        InsufficientCredits,
        NotFound,
        Unauthorized,
        Forbidden,
    )
}


def _clean(value: Any) -> str | None:
    """``value`` as a non-empty string, or ``None``.

    Anything else — a missing key, a number, Router's ``detail[]`` list form —
    reads as absent rather than being coerced, so a malformed body degrades to
    the status-derived default instead of producing a nonsense code.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def error_from_envelope(
    http_status: int,
    body: dict[str, Any] | None,
    *,
    retry_after: int | None = None,
    request_id: str | None = None,
    error_type: str | None = None,
) -> ApiError:
    """Build the typed exception for an error response.

    Falls back to a status-derived code when the body is missing or not a
    well-formed envelope (so a bare ``401`` with no JSON still maps to
    ``Unauthorized``).

    Not every route answers in the envelope shape. ``POST {router}/v2/models/{provider}/{model}``
    is fronted by Router, whose error body is ``{detail, error_type}`` and which
    repeats the same coarse bucket on the ``X-Comfy-Error-Type`` header
    (``spec/router-openapi.yaml``). Reading only ``error["code"]`` would collapse
    every one of those to the status-derived default — for a ``504`` that
    default is the meaningless ``"error"``, and ``comfy_sdk.retry`` keys its
    default-on collect rule on the bucket, so the rule would be a silent no-op
    against every real Router ``504``. ``error_type`` (the header, passed by the
    caller) and the body's own top-level ``error_type`` are read to prevent that.

    The precedence is: the envelope's ``code`` always wins; then, when the
    response identifies itself as Router's — the ``X-Comfy-Error-Type`` header
    or a top-level body ``error_type`` is present — that bucket wins for EVERY
    status; only then does :data:`_CODE_BY_STATUS` fill in, for the bare and
    proxy-shaped responses it was built for.

    The bucket outranking the status table is the load-bearing choice, learned
    three times on live traffic: the table turned Router's ``409`` errors into
    ``hash_mismatch`` (killing the contract's collect rule), its ``422``
    ``invalid_input`` into ``invalid_workflow`` (failing every reachability
    probe), and its ``403`` ``not_enabled`` into ``forbidden`` (so ``except
    NotEnabled`` — the one handler every pre-launch caller writes — never
    fired). Retyping only happens on responses that carry a bucket, which only
    Router sends, and there the bucket IS the truth; a v2 envelope carries
    ``error.code`` and no top-level ``error_type``, and an intermediary's
    reject carries neither, so both keep exactly the classes integrators
    already catch.
    """
    err = (body or {}).get("error") if isinstance(body, dict) else None
    code = (err or {}).get("code") if isinstance(err, dict) else None
    message = (err or {}).get("message") if isinstance(err, dict) else None
    details = (err or {}).get("details") if isinstance(err, dict) else None

    if code is None:
        code = _clean(error_type) or _clean(
            (body or {}).get("error_type") if isinstance(body, dict) else None
        )
    if code is None:
        code = _CODE_BY_STATUS.get(http_status)
    if code is None:
        code = "error"
    if not message:
        # Router names its human-readable string `detail`, not `error.message`.
        message = _clean((body or {}).get("detail") if isinstance(body, dict) else None)
    if not message:
        message = f"HTTP {http_status}"

    cls = _BY_CODE.get(code, ApiError)
    return cls(
        message,
        code=code,
        http_status=http_status,
        details=details if isinstance(details, dict) else None,
        retry_after=retry_after,
        request_id=request_id,
    )


_CODE_BY_STATUS: dict[int, str] = {
    401: "unauthorized",
    402: "insufficient_credits",
    403: "forbidden",
    404: "not_found",
    409: "hash_mismatch",
    422: "invalid_workflow",
    429: "queue_full",
}
