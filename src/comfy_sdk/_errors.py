"""The one base class every ``comfy_sdk`` exception descends from.

Its own module, and a deliberately tiny one, because both exception modules
need it and one of them needs the other: :mod:`comfy_sdk.router_exceptions`
defines the classes the Router surface raises, and
:mod:`comfy_sdk.exceptions` — which owns ``to_sdk_error`` — both re-exports
several of them and maps wire codes onto them. With ``ComfyError`` living in
``exceptions`` that was a cycle, and the cycle was what forced the two
hierarchies apart in the first place: ``exceptions`` could not name a
``RouterError`` subclass, so it grew its own ``Unauthorized``, ``Forbidden``
and ``InsufficientCredits`` beside the Router ones, and ``except RouterError``
silently missed all three. Import it from either module, or from the top-level
package; ``comfy_sdk.exceptions.ComfyError`` is this exact class.
"""

from __future__ import annotations

from typing import Any


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


__all__ = ["ComfyError"]
