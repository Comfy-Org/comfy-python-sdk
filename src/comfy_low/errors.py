"""The shared error envelope mapped to a typed exception per ``code``.

This is the ``comfy_low`` (protocol) view of errors: one class per documented
error ``code``, plus a fallback. ``comfy_sdk`` re-raises these as its own
idiomatic exceptions where it adds value (e.g. ``JobFailed`` carrying node
details), but the protocol codes are defined here so the generated layer has a
stable, typed error surface.
"""

from __future__ import annotations

from typing import Any


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
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.http_status = http_status
        self.details = details
        self.retry_after = retry_after


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
    error_type: str | None = None,
) -> ApiError:
    """Build the typed exception for an error response.

    Falls back to a status-derived code when the body is missing or not a
    well-formed envelope (so a bare ``401`` with no JSON still maps to
    ``Unauthorized``).

    Not every route answers in the envelope shape. ``POST /api/v2/models/run``
    is fronted by Router, whose error body is ``{detail, error_type}`` and which
    repeats the same coarse bucket on the ``X-Comfy-Error-Type`` header
    (``spec/router-openapi.yaml``). Reading only ``error["code"]`` would collapse
    every one of those to the status-derived default — for a ``504`` that
    default is the meaningless ``"error"``, and ``comfy_sdk.retry`` keys its
    default-on collect rule on the bucket, so the rule would be a silent no-op
    against every real Router ``504``. ``error_type`` (the header, passed by the
    caller) and the body's own top-level ``error_type`` are read to prevent that.

    They are consulted in one narrow place, though: *after* the envelope's
    ``code``, which always wins, and *after* :data:`_CODE_BY_STATUS`, which
    already determines a code for every status where this API has a documented
    one. So a Router ``429`` still maps to ``queue_full`` and a Router ``401``
    still to ``unauthorized`` — reordering those would silently retype
    exceptions that integrators already catch. What is left is exactly the 5xx
    range, where the status determines nothing and the bucket is the only name
    the response has.
    """
    err = (body or {}).get("error") if isinstance(body, dict) else None
    code = (err or {}).get("code") if isinstance(err, dict) else None
    message = (err or {}).get("message") if isinstance(err, dict) else None
    details = (err or {}).get("details") if isinstance(err, dict) else None

    if code is None:
        code = _CODE_BY_STATUS.get(http_status)
    if code is None:
        code = _clean(error_type) or _clean(
            (body or {}).get("error_type") if isinstance(body, dict) else None
        )
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
