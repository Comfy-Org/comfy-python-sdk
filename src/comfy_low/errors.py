"""The shared error envelope mapped to a typed exception per ``code``.

This is the ``comfy_low`` (protocol) view of errors: one class per documented
error ``code``, plus a fallback. ``comfy_sdk`` re-raises these as its own
idiomatic exceptions where it adds value (e.g. ``JobFailed`` carrying node
details), but the protocol codes are defined here so the generated layer has a
stable, typed error surface.
"""

from __future__ import annotations

import re
import unicodedata
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


#: Longest body excerpt kept on an exception. Long enough for the one-line
#: reason an intermediary states (``no healthy upstream``, ``upstream connect
#: error or disconnect/reset before headers``), short enough that an HTML error
#: page cannot flood the log line that prints it.
_BODY_EXCERPT_LIMIT = 256

#: Unicode general categories replaced by a space in a body excerpt. The
#: excerpt is server-controlled text headed for a traceback or a log line, so
#: it is reduced to something safe to *display* for the same reason
#: ``clean_request_id`` bounds the id: an escape sequence in an error page must
#: not repaint the terminal reading it. ``Cc`` is the C0/C1 control range (the
#: C1 half is where a stray byte of Latin-1-decoded binary lands); ``Cf`` is the
#: format characters — bidi overrides that reverse how a log line reads,
#: zero-width joiners and spaces that hide a break, the BOM; ``Co`` and ``Cs``
#: are private-use and lone surrogates, which no error page has a reason to
#: contain and no terminal has a glyph for.
_UNPRINTABLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})

#: How much of a body is walked to produce an excerpt, as a multiple of the
#: excerpt limit. Collapsing whitespace and mapping characters is linear in the
#: input, and an error page can be megabytes; the reduction has to cost the
#: same for a 2 KiB body and a 20 MiB one.
_BODY_EXCERPT_WINDOW = 8


def clean_body_excerpt(raw: Any, *, limit: int = _BODY_EXCERPT_LIMIT) -> str | None:
    """``raw`` reduced to a bounded single-line excerpt, or ``None``.

    Unprintable characters become spaces rather than vanishing, so ``no
    healthy\\x00upstream`` reads ``no healthy upstream`` and not ``no
    healthyupstream``. Whitespace of every kind then collapses to single spaces
    before the bound is applied, so an indented HTML error page spends its
    budget on words rather than on the margin, and the result is one line
    wherever it is printed. Truncation is deliberately silent — no ellipsis — so
    the value stays exactly what the server said, up to ``limit`` characters of
    it.

    Only a bounded head of ``raw`` is examined (leading whitespace aside, which
    is stripped first so an indented page still yields its words): a few
    kilobytes is enough to fill a 256-character excerpt of any real error page,
    and it keeps the cost of describing a huge body independent of its size.
    """
    if not isinstance(raw, str):
        return None
    head = raw.lstrip()[: limit * _BODY_EXCERPT_WINDOW]
    printable = "".join(
        " " if unicodedata.category(ch) in _UNPRINTABLE_CATEGORIES else ch for ch in head
    )
    collapsed = " ".join(printable.split())
    return collapsed[:limit] or None


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
        body_excerpt: str | None = None,
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
        #: A bounded, single-line excerpt of a response body that stated no
        #: message of its own, or ``None``. It is the only place such a response
        #: states its cause: something in front of the API — a load balancer, a
        #: proxy — answers with plain text (``no healthy upstream``, ``upstream
        #: connect error or disconnect/reset before headers``) and no JSON, and
        #: that text used to be discarded with the response, leaving the cause
        #: unrecoverable from any log. ``None`` whenever the response *did*
        #: state a message — an envelope's ``error.message``, Router's
        #: ``detail`` — because there ``message`` already carries the cause;
        #: :func:`error_from_envelope` enforces that, so a set excerpt always
        #: means ``message`` is one this SDK synthesised.
        self.body_excerpt = body_excerpt

    def __str__(self) -> str:
        """``message``, plus the body excerpt whenever there is one.

        An excerpt is only ever kept beside a message this SDK made up — the
        ``HTTP <status>`` of a response with no envelope, the "could not decode"
        of a success whose body was not JSON — and such a message names no
        cause. Appending the excerpt is what puts the cause into the one string
        a caller prints and a logger formats: ``HTTP 503: no healthy upstream``.
        A message the server actually sent never has an excerpt beside it (see
        :attr:`body_excerpt`), so it is returned unchanged.
        """
        if self.body_excerpt:
            return f"{self.message}: {self.body_excerpt}"
        return self.message


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
    body_excerpt: str | None = None,
) -> ApiError:
    """Build the typed exception for an error response.

    Falls back to a status-derived code when the body is missing or not a
    well-formed envelope (so a bare ``401`` with no JSON still maps to
    ``Unauthorized``).

    ``body_excerpt`` is the caller's already-reduced excerpt of the body
    (:func:`clean_body_excerpt`). It is kept on the exception only when no
    message could be read from the body — a response with no envelope states
    its cause nowhere else, while one that carried ``error.message`` or Router's
    ``detail`` has already said everything it is going to say, and a second copy
    of the same text helps nobody. The gate lives here rather than at the raise
    site because this is the function that knows whether a message was found:
    ``{"message": "no healthy upstream"}`` is a JSON object and still not an
    envelope. The text itself is the caller's to pass rather than this
    function's to read: only the caller holds the response, and only it can
    tell an unread streaming body from an empty one.

    Not every route answers in the envelope shape. ``POST {router}/v2/models/{provider}/{model}``
    is fronted by Router, whose error body is ``{detail, error_type}`` and which
    repeats the same coarse bucket on the ``X-Comfy-Error-Type`` header
    (``spec/router-openapi.yaml``). Reading only ``error["code"]`` would collapse
    every one of those to the status-derived default — for a ``504`` that
    default names only the status (``"http_504"``), and ``comfy_sdk.retry`` keys
    its default-on collect rule on the bucket, so the rule would be a silent
    no-op against every real Router ``504``. ``error_type`` (the header, passed by the
    caller) and the body's own top-level ``error_type`` are read to prevent that.

    The precedence is: the envelope's ``code`` always wins; then, when the
    response identifies itself as Router's — the ``X-Comfy-Error-Type`` header
    or a top-level body ``error_type`` is present — that bucket wins for EVERY
    status; only then does :data:`_CODE_BY_STATUS` fill in, for the bare and
    proxy-shaped responses it was built for; and a status that table does not
    name falls back to ``http_<status>`` (see below).

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
    # Through `_clean` like every other string read off the wire: `message` ends
    # up as `str(exc)`, and a non-string here (a list, a number) would make that
    # raise `TypeError: __str__ returned non-string` at the one moment — inside
    # a logger or a traceback — where an exception must not fail.
    message = _clean((err or {}).get("message") if isinstance(err, dict) else None)
    details = (err or {}).get("details") if isinstance(err, dict) else None

    if code is None:
        code = _clean(error_type) or _clean(
            (body or {}).get("error_type") if isinstance(body, dict) else None
        )
    if code is None:
        code = _CODE_BY_STATUS.get(http_status)
    if code is None:
        # Nothing identified this response: no envelope ``code``, no Router
        # bucket, and a status :data:`_CODE_BY_STATUS` does not name. ``"error"``
        # was the answer here, and it was indistinguishable from the class
        # default an exception built by hand carries — so it told a caller
        # nothing at all, on the one class of failure where the SDK has nothing
        # else to say. The status is the single fact such a response does carry,
        # so it becomes the code. The shape is deliberately prefixed rather than
        # bare: ``http_503`` reads as "no service verdict was reached — something
        # in front of the API answered", which is exactly what it means, and it
        # cannot collide with a wire ``code`` or a Router bucket, none of which
        # are spelled that way.
        code = f"http_{http_status}"
    if not message:
        # Router names its human-readable string `detail`, not `error.message`.
        message = _clean((body or {}).get("detail") if isinstance(body, dict) else None)
    if message:
        # The response stated its cause; the excerpt would be a second copy of
        # it (or of the envelope around it). See the docstring.
        body_excerpt = None
    else:
        message = f"HTTP {http_status}"

    cls = _BY_CODE.get(code, ApiError)
    return cls(
        message,
        code=code,
        http_status=http_status,
        details=details if isinstance(details, dict) else None,
        retry_after=retry_after,
        request_id=request_id,
        body_excerpt=body_excerpt,
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
