"""Handles for *queued* model requests — ``client.models.submit`` and friends.

The queued form of a model run. ``models.run`` holds one connection open until
the generation is finished; ``models.submit`` hands back a
:class:`RequestHandle` the moment the server accepts the request, and the
generation is collected later — from another coroutine, another process, or
another machine, since a handle is rehydratable from nothing but the model id
and the request id (``client.models.handle``).

**The server owns the queue.** Ordering, admission, retries, timeouts, billing
and expiry are all decided server side; this module adds polling and ergonomics
and nothing else. Anything here that looked like queue *behaviour* — a local
position estimate, a client-side retry of a rejected submit, an expiry clock —
would be a second, disagreeing implementation of a decision that has already
been made somewhere authoritative.

The shape is deliberately the one :mod:`comfy_sdk.jobs` already uses for
workflow jobs — submit, hold a handle, poll to a terminal state with adaptive
backoff, collect or cancel — rather than a second idiom for the same thing. Two
differences follow from the surface rather than from taste:

* **Terminal means ``COMPLETED``, and a failure is a completion.** The server
  reports a failed or cancelled request as ``COMPLETED`` carrying an
  ``error_type``, so a ``200`` is not the same thing as a success. Every path
  that reads a completion runs it through
  :func:`~comfy_sdk.router_exceptions.error_from_completion` and raises the
  typed router exception, which is what keeps a failed generation from being
  returned as a result. A status this SDK version has never heard of is treated
  as *not yet terminal* — the set grows on the server's release cycle, and
  guessing that an unknown state is finished would collect a result that does
  not exist yet.

* **There is no event stream.** :meth:`RequestHandle.iter_events` is the poll
  loop with its updates exposed, not SSE — the queue's streaming surfaces are
  not part of this. Polls are paced by the server's own ``Retry-After`` when it
  names one and by an adaptive backoff when it does not.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from comfy_low.errors import ApiError
from comfy_low.transport import AsyncComfyLow, ComfyLow, parse_request_id

from . import _core
from .exceptions import ComfyError, translating
from .retry import DEFAULT_RETRY, NO_RETRY, Retrier, RetryPolicy, error_bucket_of
from .router_exceptions import Cancelled, CancelRefused, RouterError, error_from_completion

#: The one terminal queue status. Deliberately a single value rather than a
#: :data:`comfy_sdk._core.TERMINAL`-style set: the server does not express a
#: cancel or a failure as its own status, it expresses them as this status plus
#: an ``error_type``. Adding "CANCELED" here on the assumption it exists would
#: strand a caller whose request really did reach ``COMPLETED``.
COMPLETED = "COMPLETED"

#: A request admitted to the queue that has not been dispatched to a partner
#: yet. One of the three values of the vendored contract's ``RouterQueueStatus``
#: (``spec/router-openapi.yaml``, the ``RouterQueueStatus`` schema), which is a
#: **closed** ``enum`` on purpose: the spec says so in its own description,
#: because a lifecycle that grows a fourth state is a breaking change to every
#: polling loop written against it whether it is declared as an enum or not.
#: It is exported because a caller comparing :attr:`QueueUpdate.status` wants
#: the contract's own spelling rather than a literal of their own -- and
#: because it is the one live status with a billing consequence attached: the
#: ``cancelled`` bucket's meaning pins that a request cancelled while it was
#: still ``IN_QUEUE`` "was never dispatched and cannot be charged".
IN_QUEUE = "IN_QUEUE"

#: A request a partner is generating for. The second of
#: :data:`IN_QUEUE`/:data:`IN_PROGRESS`/:data:`COMPLETED`, exported for the
#: same reason. Unlike :data:`IN_QUEUE` it carries no unbilled guarantee: the
#: ``cancelled`` bucket says a request cancelled after it was admitted "may
#: still be charged", so a cancel landing here is a detach, not a free stop.
IN_PROGRESS = "IN_PROGRESS"

#: What the cancel route's ``202`` names itself (``spec/router-openapi.yaml``,
#: the ``RouterCancelStatus`` schema and the ``cancelRouterModelRequest``
#: ``202`` description). Deliberately NOT exported and deliberately not part of
#: :data:`IN_QUEUE`/:data:`IN_PROGRESS`/:data:`COMPLETED`: it is a value of the
#: cancel body's own vocabulary, not a queue state, and a caller who compared
#: :attr:`QueueUpdate.status` against it would be comparing against something
#: the status route can never answer. It means the ask was accepted for a
#: request that had not yet reached a terminal state -- not that the run
#: stopped -- so this SDK confirms it with one status read; see
#: :func:`_reading_of_accepted_cancel`.
_CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"

#: The code the SDK raises from the timeout teardown when the cancel route
#: answered its ``202`` and the confirming poll still found the request
#: :data:`IN_QUEUE`. The contract does not allow that ordering -- the route
#: writes the row terminal *before* it answers -- so it is a server that
#: violated its own write order, and the honest report is that the cancel did
#: not land. Reported rather than detached because the spec pins an
#: ``IN_QUEUE`` request as never dispatched and unchargeable, and a
#: :class:`DetachedRequest` asserts the opposite.
_CANCEL_NOT_APPLIED_CODE = "cancel_not_applied"

#: Failures a poll is retried through, matching ``models.run``'s tuple exactly.
#: Being listed is not "retryable" — :meth:`~comfy_sdk.retry.RetryPolicy.should_retry`
#: decides that. What it buys is that anything else propagates untouched.
_CANDIDATE_FAILURES = (ApiError, RouterError, httpx.TransportError)

#: What a best-effort cancel is allowed to fail with. Narrow on purpose: this
#: is only ever used to keep a cancel from masking the ``TimeoutError`` that
#: prompted it, and swallowing a bare ``Exception`` there would hide a bug in
#: this SDK just as readily as it hides an unreachable server.
_CANCEL_FAILURES = (ComfyError, httpx.HTTPError)

#: Ceiling, in seconds, on a server-named ``Retry-After`` between two polls.
#: The header is honoured because the server knows its own pace, but it is a
#: hint and not a bound, and taking it verbatim would let one ``Retry-After:
#: 86400`` park a thread for a day -- or an absurd-but-parseable value overflow
#: ``float()``. A minute is long enough that a request told to wait longer is
#: still polled rarely, and short enough that nothing is parked.
_MAX_PACE = 60

#: Floor, in seconds, on the per-request HTTP timeout derived from a caller's
#: remaining deadline. A sub-second bound cannot complete a TLS handshake, so
#: without the floor the last poll before a deadline would be a certain
#: transport failure rather than an answer.
_MIN_HTTP_TIMEOUT = 1.0

#: HTTP timeout, in seconds, for the best-effort cleanup cancel ``subscribe``
#: issues after its own timeout -- see ``RequestHandle._cancel_best_effort``.
_CANCEL_TIMEOUT = 10.0

#: The status a cancel is refused with when the REQUEST'S OWN STATE is what
#: refuses it, rather than the caller, the credential or the server. It is the
#: plain HTTP reading of ``409`` -- the target's current state conflicts with
#: the operation -- and that reading is what this keys on, deliberately rather
#: than on the refusal's prose, which no contract pins.
#:
#: **The route emits exactly one such ``409``, and it is typed.**
#: ``ALREADY_COMPLETED`` is the whole of it, and the vendored contract says so:
#: ``cancelRouterModelRequest`` in ``spec/router-openapi.yaml`` declares a
#: ``202`` and that one ``409``, and describes the ``409`` as "the request had
#: already reached a terminal state, so there was nothing to cancel". The
#: cancellation write is guarded on the request being NON-terminal, so a
#: request in either live state is CANCELLED rather than refused and the
#: ``409`` is reached only when that guard matched nothing. **There is no
#: in-flight ``409``.** It reaches this SDK as
#: :class:`~comfy_sdk.router_exceptions.AlreadyCompleted` under the
#: :class:`~comfy_sdk.router_exceptions.CancelRefused` base, and
#: :func:`_refused_on_state` asks that typed question FIRST, because branching
#: on the class is strictly better than branching on a status.
#:
#: The status clause below it therefore has no producer today. It is kept, and
#: it is kept NARROW: it matches a ``409`` naming no bucket at all, so a
#: ``409`` the contract DOES name -- ``invalid_input``,
#: ``concurrency_limit_exceeded`` -- is not swept in with it. What it buys is
#: failing closed on a deployment that refuses on state without a bucket, in
#: either direction: such a refusal reads as "the run did not stop" and is
#: confirmed by a poll, rather than escaping as an unexplained cancel failure.
#: If the route is ever given a second state refusal with a bucket of its own,
#: add the subclass to
#: :data:`~comfy_sdk.router_exceptions.CANCEL_REFUSALS` and this status clause
#: can go.
_CANCEL_REFUSED_STATUS = 409

#: The code :mod:`comfy_low.errors` synthesises for a ``409`` that identified
#: itself with nothing -- no envelope ``code``, no Router bucket. Reading it as
#: "named no bucket" is what keeps :func:`_refused_on_state` matching the
#: in-flight refusal's bucket-less shape while still failing closed on a
#: ``409`` that names a real one.
_UNIDENTIFIED_REFUSAL_CODE = f"http_{_CANCEL_REFUSED_STATUS}"

_now = time.monotonic


@dataclass(frozen=True)
class QueueUpdate:
    """One observation of a queued request's place in the queue.

    What :meth:`RequestHandle.status` returns, what
    :meth:`RequestHandle.iter_events` yields, and what ``subscribe``'s
    ``on_queue_update`` callback is handed.

    ``status`` is an **open string**, not an enum: a status added server side
    has to reach the caller as itself rather than as a decoding failure. Compare
    it against :data:`COMPLETED`, or read :attr:`is_completed`.
    """

    #: The request id this update is about — the same id
    #: ``client.models.handle`` rehydrates from.
    request_id: str
    #: The server's status for the request, verbatim. ``""`` when the response
    #: named none at all (a cancel answered ``204``, say).
    status: str
    #: Position in the queue when the server reported one, else ``None``. It is
    #: the server's number, never computed here.
    queue_position: int | None = None
    #: The failure bucket a completion carries, when it carries one. Present
    #: here as data; the raising is done by the methods that collect a result.
    error_type: str | None = None
    #: Seconds the server asked the caller to wait before polling again, from
    #: ``Retry-After``. ``None`` when it named no pace, in which case the
    #: adaptive backoff decides.
    retry_after: int | None = None
    #: The decoded response body, unmodified — the escape hatch for a field
    #: this dataclass does not model yet.
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        """Whether the request has reached the queue's one terminal status."""
        return self.status == COMPLETED

    def __repr__(self) -> str:
        position = "" if self.queue_position is None else f", queue_position={self.queue_position}"
        error = "" if self.error_type is None else f", error_type={self.error_type!r}"
        return (
            f"QueueUpdate(request_id={self.request_id!r}, status={self.status!r}{position}{error})"
        )


def _retry_after_seconds(headers: httpx.Headers) -> int | None:
    """``Retry-After`` as a positive whole number of seconds, or ``None``.

    Non-positive and unparseable values are dropped rather than honoured, for
    the reason :class:`~comfy_sdk.retry.Retrier` gives for the same check: a
    pace of zero names no pace, and taking it verbatim turns a server that
    keeps answering it into a zero-delay poll loop.
    """
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _text(value: Any) -> str | None:
    """A body field as a non-empty, stripped string, or ``None``.

    The same reading :func:`~comfy_sdk.router_exceptions.error_from_completion`
    gives ``error_type``, so an update and the raising path cannot disagree
    about whether a blank bucket is a failure.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _update_from(
    payload: Any, headers: httpx.Headers, *, request_id: str, require_status: bool = False
) -> QueueUpdate:
    """Build a :class:`QueueUpdate` from one response.

    ``request_id`` is the id the call was addressed by, and it is the one the
    update carries: the body's copy is server-controlled and unvalidated, and
    an update that named a different request from the one it was asked about
    would be wrong in exactly the place a caller pastes into a support ticket.
    It is also what keeps an update from a body-less ``204`` cancel still
    identifying the request it is about.

    ``require_status`` is set by the authoritative status read, where a body
    naming no status is not a state to poll again but a response this SDK
    cannot act on -- treating it as "not yet terminal" would poll a ``200 {}``
    forever. It stays off for the cancel, whose ``204`` legitimately names none.
    """
    if not isinstance(payload, Mapping):
        raise ComfyError(
            "the queue answered with a body that is not a JSON object, so the request's "
            "state cannot be read from it",
            code="invalid_response",
        )
    status = payload.get("status")
    if not isinstance(status, str):
        status = ""
    if require_status and not status.strip():
        raise ComfyError(
            "the status read answered without naming a status, so the request's state is unknown",
            code="invalid_response",
        )
    position = payload.get("queue_position")
    return QueueUpdate(
        request_id=request_id,
        status=status,
        queue_position=position
        if isinstance(position, int) and not isinstance(position, bool)
        else None,
        error_type=_text(payload.get("error_type")),
        retry_after=_retry_after_seconds(headers),
        raw=dict(payload),
    )


def _request_id_of(payload: Any) -> str:
    """The request id a submit response names, or a :class:`ComfyError`.

    A submit whose response carries no usable id is unusable in the specific
    way that matters here: the work may well have been accepted and billed, and
    the caller has been left with no way to reach it. That is a failure of the
    call, so it is raised rather than papered over with a placeholder id that
    would 404 on the first poll.
    """
    if not isinstance(payload, Mapping):
        raise ComfyError(
            "the queue accepted the request but answered with a body that is not a JSON "
            "object, so no request_id could be read from it",
            code="invalid_response",
        )
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ComfyError(
            "the queue accepted the request but its response named no request_id, so the "
            "request cannot be polled, collected or cancelled",
            code="invalid_response",
        )
    try:
        # The id becomes a path segment on every later call, so it is held to
        # the same rule a caller-supplied one is. Failing here rather than on
        # the first poll keeps the failure next to the response that caused it
        # — and next to the `Idempotency-Key` that can recover the request.
        return parse_request_id(request_id)
    except ValueError as exc:
        raise ComfyError(
            f"the queue named a request_id that cannot address a route: {exc}",
            code="invalid_response",
        ) from exc


def _raise_for_completion(payload: Any, *, request_id: str, envelope_only: bool = False) -> None:
    """Raise the typed router exception a completion reports, if it reports one.

    The gate behind "a ``200`` with an error payload is never returned as
    success". It runs on the terminal status read *and* on the collected
    result, because either can be the response that carries the ``error_type``
    and checking only one leaves the other handing a failure back as data.

    ``envelope_only`` is for the result route, and it is a deliberate
    narrowing rather than caution. That route's body is the **provider's own
    payload**, forwarded verbatim — a partner model is free to have a field
    called ``error_type`` in its native output, and turning one of those into a
    raised exception would fail a generation that succeeded. So on that body an
    ``error_type`` only counts when it arrives inside the queue's own envelope,
    which is what a ``COMPLETED`` status alongside it identifies. The status
    read has no such ambiguity and is checked unconditionally, so the failure
    the server reports where it is authoritative is never the one that gets
    missed.
    """
    if not isinstance(payload, Mapping):
        # A partner's native output is whatever JSON document the partner
        # answers with -- an array or a bare value is a result, not an
        # envelope, and there is nothing in it the queue could have reported.
        return
    if envelope_only and payload.get("status") != COMPLETED:
        return
    error = error_from_completion(payload, request_id=request_id)
    if error is not None:
        raise error


def _changed(previous: QueueUpdate | None, current: QueueUpdate) -> bool:
    """Whether ``current`` is worth reporting given ``previous``.

    The first observation always is. After that, only a change in the two
    fields a caller renders — the status and the queue position — counts, so a
    progress bar is not redrawn once a second for a request that has not moved.
    """
    if previous is None:
        return True
    return (previous.status, previous.queue_position) != (current.status, current.queue_position)


def _pace(update: QueueUpdate, backoff: Iterator[float]) -> float:
    """Seconds to wait before the next poll.

    A pace the server named beats the schedule guessed here — that is the whole
    point of ``Retry-After`` — and the adaptive backoff carries the interval
    when it named none. The backoff is advanced either way so the schedule does
    not restart from its floor the moment the server stops naming a pace. The
    named pace is capped at :data:`_MAX_PACE`: a hint, honoured, but not a bound
    a single header can park the caller behind.
    """
    scheduled = next(backoff)
    if update.retry_after is None:
        return scheduled
    return float(min(update.retry_after, _MAX_PACE))


def _remaining(deadline: float | None) -> float | None:
    """Seconds left before ``deadline``, or ``None`` when there is no deadline."""
    return None if deadline is None else deadline - _now()


def _completed(update: QueueUpdate | None) -> QueueUpdate:
    """The terminal update the poll loop ended on.

    ``iter_events`` always yields the completion — the status it carries
    differs from every update before it, and the loop only returns once it has
    seen one — so ``None`` here is unreachable rather than a state to handle. It
    is still checked, because the alternative is a ``None`` dereference in the
    middle of collecting a result if that ever stops being true.
    """
    if update is None:  # pragma: no cover - unreachable; see the docstring
        raise ComfyError(
            "the poll loop ended without observing a completion", code="invalid_response"
        )
    return update


def _last(updates: Iterator[QueueUpdate]) -> QueueUpdate:
    """Drain ``updates`` and return the final one — the completion."""
    final: QueueUpdate | None = None
    for update in updates:
        final = update
    return _completed(final)


def _timed_out(request_id: str, timeout: float | None, update: QueueUpdate | None) -> TimeoutError:
    status = "unknown" if update is None else update.status
    return TimeoutError(
        f"model request {request_id} not complete after {timeout}s (status={status!r})"
    )


class SubscribeTimeout(TimeoutError):
    """``models.subscribe``'s own timeout, saying what became of the request.

    A ``TimeoutError`` subclass rather than a new hierarchy, so every
    ``except TimeoutError`` already written around ``subscribe`` keeps catching
    it and keeps reading the same message. What it adds is the state a caller
    otherwise had to guess at: whether the cleanup cancel actually stopped the
    run, and the two ids that reach it again if it did not.

    It is raised for the two timeout outcomes that leave nothing to collect.
    The third -- the queue refused the cancel because the run was already in
    flight -- is not an error at all and is *returned* as a
    :class:`DetachedRequest`; see :meth:`comfy_sdk.models.Models.subscribe`.
    """

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        model: str,
        cancelled: bool,
        cancel_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        #: The request the timed-out call was following. Pass it and
        #: :attr:`model` to ``client.models.handle`` to reach it again.
        self.request_id = request_id
        #: The canonical ``{provider}/{model}`` id the request was submitted to.
        self.model = model
        #: ``True`` only when the request's own row confirmed the stop --
        #: ``COMPLETED`` carrying the ``cancelled`` bucket -- or when a
        #: body-less accepted cancel left nothing to confirm against. The run
        #: is then gone and there is nothing left to collect. It is not by
        #: itself a statement about the charge: a request cancelled while
        #: still ``IN_QUEUE`` was never dispatched and cannot be charged,
        #: while one cancelled after admission may still be.
        #:
        #: ``False`` means the cancel failed, or was accepted and did not
        #: apply, so the run's fate is unknown -- read :attr:`cancel_error`,
        #: and treat the request as possibly still running.
        self.cancelled = cancelled
        #: The failure the cleanup cancel raised, when it raised one. Also on
        #: ``__cause__``. ``None`` when the cancel was accepted. It is surfaced
        #: rather than swallowed because a cancel that failed on a transport
        #: error or a rejected credential says the run may well still be
        #: running -- and billing -- which is the opposite of what a bare
        #: timeout has always implied here.
        self.cancel_error = cancel_error

    def __reduce__(
        self,
    ) -> tuple[Callable[..., SubscribeTimeout], tuple[Any, ...]]:
        """Rebuild through the real constructor, keeping every field.

        ``BaseException.__reduce__`` reconstructs from :attr:`args` alone, and
        ``args`` is just ``(message,)`` here -- so the inherited one rebuilds
        this as ``SubscribeTimeout(message)`` and dies on the three required
        keyword-only arguments. That turns a pickle or a ``copy.copy`` into a
        ``TypeError`` that masks the real failure, in exactly the
        cross-process workflow this queued surface exists for: submit here,
        collect in a worker pool or a task queue, where :attr:`request_id` is
        the only route back to a generation that is still billing.
        """
        return (
            _rebuild_subscribe_timeout,
            (str(self), self.request_id, self.model, self.cancelled, self.cancel_error),
        )


def _refused_on_state(exc: BaseException) -> bool:
    """Whether a failed cancel was the queue refusing on the REQUEST's state.

    Two ways in, and the typed one is preferred wherever it is available:

    * a :class:`~comfy_sdk.router_exceptions.CancelRefused` -- the cancel
      route's refusals that this SDK version recognises and types, which today
      is :class:`~comfy_sdk.router_exceptions.AlreadyCompleted`. Branching on
      the class is the honest test, and it costs nothing to ask first.
    * failing that, a :data:`_CANCEL_REFUSED_STATUS` that names **no bucket at
      all**, reaching this SDK as a bare
      :class:`~comfy_sdk.exceptions.ComfyError`. The shipped route emits no
      such refusal -- ``ALREADY_COMPLETED`` is its only ``409`` and it is typed
      -- so this clause is a fallback with no producer today, kept because it
      fails closed: see :data:`_CANCEL_REFUSED_STATUS`.

    The second clause **fails closed**, which is the whole of the narrowing: a
    ``409`` that DOES name a bucket is something the contract already has a
    name for -- ``invalid_input``, or ``concurrency_limit_exceeded`` with its
    ``Retry-After`` -- and is not the state refusal, so it surfaces on
    :attr:`SubscribeTimeout.cancel_error` instead of being reported as a detach
    that asserts the run is in flight and billed. Two nearby precedents read
    ``409`` the same way: :func:`comfy_sdk.retry.is_collectable` gates it on
    status *and* bucket, and ``comfy_low.errors._CODE_BY_STATUS`` omits it
    outright because the status alone is ambiguous.

    "Names no bucket" has two spellings, because the layer below fills the
    gap rather than leaving it: a response that identified itself with
    nothing at all is given the synthetic code ``http_<status>``
    (:mod:`comfy_low.errors`), which is documented there as unable to collide
    with a wire ``code`` or a Router bucket precisely because nothing real is
    spelled that way. So both ``None`` and that sentinel mean the same thing
    here, and anything else is a bucket the server really did name.

    ``getattr`` because the transport-level failures in
    :data:`_CANCEL_FAILURES` are httpx's own classes and carry no
    ``http_status`` at all; those are exactly the ones that must not be
    swallowed, so their absence reading as "not a refusal" is the right answer.
    """
    if isinstance(exc, CancelRefused):
        return True
    if getattr(exc, "http_status", None) != _CANCEL_REFUSED_STATUS:
        return False
    bucket = error_bucket_of(exc)
    return bucket is None or bucket == _UNIDENTIFIED_REFUSAL_CODE


def _rebuild_subscribe_timeout(
    message: str,
    request_id: str,
    model: str,
    cancelled: bool,
    cancel_error: BaseException | None,
) -> SubscribeTimeout:
    """Unpickle a :class:`SubscribeTimeout`; see its ``__reduce__``.

    Module-level because pickle has to be able to name it, and a plain
    function rather than the class itself because the fields are keyword-only
    and ``__reduce__``'s callable is applied to a positional tuple.
    """
    return SubscribeTimeout(
        message,
        request_id=request_id,
        model=model,
        cancelled=cancelled,
        cancel_error=cancel_error,
    )


class _CancelReading(enum.Enum):
    """What a cancel the route ACCEPTED says about whether the run stopped."""

    #: The run is gone. Nothing is left to collect and nothing more is billed.
    STOPPED = "stopped"
    #: The run finished on its own. There IS a result, and it is already paid
    #: for, so it is collected rather than discarded.
    FINISHED = "finished"
    #: The route took the message but the run has NOT stopped -- a ``200`` or
    #: ``202`` echoing a live queue state, which is not a shape the shipped
    #: route produces. Confirmed against the server, then reported as a detach.
    UNSTOPPED = "unstopped"
    #: The contract's own answer: a ``202`` naming
    #: :data:`_CANCELLATION_REQUESTED`. The route has ALREADY written the row
    #: terminal by the time it answers this, but the body says nothing about
    #: which terminal state, so it is confirmed by one status read -- and
    #: unlike :attr:`UNSTOPPED` that read is expected to find a stop.
    ACCEPTED = "accepted"


def _reading_of_accepted_cancel(accepted: QueueUpdate) -> _CancelReading:
    """Read a 2xx cancel's own body for whether the work actually stopped.

    A 2xx on the cancel route says the server accepted the *message*, which is
    not the same as the run having stopped -- the binding accepts ``200``,
    ``202`` and ``204`` alike, and :meth:`cancel`'s docstring already tells its
    own callers to "read the returned update's status ... rather than assuming
    the work stopped". The timeout teardown used to discard that body and
    assert ``cancelled=True`` from the bare fact of a 2xx, which is the one
    reading the response does not support.

    Four readings, from the update the cancel answered with:

    * no status at all (the body-less ``204`` a legacy accepted cancel is) --
      :attr:`~_CancelReading.STOPPED`. There is nothing to read, and an
      accepted cancel on an undispatched request is what that shape means;
      spending another round trip to re-confirm it would charge every timeout
      for a shape the current contract does not even emit.
    * :data:`_CANCELLATION_REQUESTED` -- :attr:`~_CancelReading.ACCEPTED`, the
      contract's own ``202``. The route only answers it once its guarded
      ``UPDATE`` has already written the row terminal, so the ask DID land;
      what the body does not say is which terminal state, and the ``cancelled``
      bucket's meaning makes that a billing question. So it is confirmed by one
      status read rather than asserted. Compared case-sensitively against the
      raw string, because :func:`_update_from` neither trims nor folds it and a
      value that differs in case is a different value.
    * a terminal :data:`COMPLETED` carrying an ``error_type`` --
      :attr:`~_CancelReading.STOPPED`. Terminal, and the bucket is how this
      queue expresses a stop (it has no ``CANCELED`` status of its own; see
      :data:`COMPLETED`).
    * a terminal :data:`COMPLETED` carrying NO bucket --
      :attr:`~_CancelReading.FINISHED`. It ran to completion and was billed, so
      there is a real result behind it.
    * anything else, which is a non-terminal status on a live request --
      :attr:`~_CancelReading.UNSTOPPED`. No shipped deployment answers this;
      it is what a deployment that echoed a queue state would land on.
    """
    if not accepted.status:
        return _CancelReading.STOPPED
    if accepted.status == _CANCELLATION_REQUESTED:
        return _CancelReading.ACCEPTED
    if not accepted.is_completed:
        return _CancelReading.UNSTOPPED
    return _CancelReading.STOPPED if accepted.error_type is not None else _CancelReading.FINISHED


def _is_cancelled_completion(update: QueueUpdate) -> bool:
    """Whether a terminal update is a request the cancel route STOPPED.

    The queue has no ``CANCELED`` status of its own (see :data:`COMPLETED`), so
    a stop is :data:`COMPLETED` plus the contract's ``cancelled`` bucket --
    which is precisely what the cancel route writes before it answers its
    ``202`` (``error_type=cancelled`` on the guarded ``UPDATE``).

    It matters that this is told apart from every other terminal
    ``error_type``: a run that failed on its own should raise its typed error,
    which is what :meth:`RequestHandle._collect_or_detach` does. A stop the SDK
    itself asked for is not a failure to report -- it is the answer to the ask
    -- so it becomes the cancelled ending instead.

    One function rather than the expression inline in both handles, because
    ``tests/test_sync_async_parity.py`` walks the sync and async surfaces
    together and a predicate spelled twice is a predicate that drifts once.
    """
    return update.is_completed and update.error_type == Cancelled.error_type


def _cancel_not_applied(request_id: str, status: str) -> ComfyError:
    """The failure a cancel that did not take effect reports as.

    Raised from :meth:`RequestHandle._detach_report` whenever the confirming
    poll finds the request still :data:`IN_QUEUE`, however that poll was
    reached. On the contract path it means a server answered its ``202`` out
    of its own documented order -- the cancellation write is guarded on the
    request being non-terminal and lands BEFORE the ``202``; on the
    bucket-less-``409`` fallback it means a refusal that left the request
    exactly where it was. Either way the ask did not take effect, which is not
    something to paper over with a detach: the contract pins an ``IN_QUEUE``
    request as unbilled, and a detach claims the opposite.

    The message names the request id and quotes the status VERBATIM rather
    than describing it, because the enum is closed and a value outside it is
    the most useful thing the message could carry -- and ``subscribe`` folds
    this onto :attr:`SubscribeTimeout.cancel_error`, where it is the caller's
    only account of what the cancel did.

    :attr:`~comfy_sdk.exceptions.ComfyError.request_id` is deliberately left
    unset: that field is the server-minted ``X-Comfy-Request-Id`` of one HTTP
    call, which is not the same identifier as the queued request's own id, and
    filling it with the latter would make the two indistinguishable. The queued
    id reaches the caller on :attr:`SubscribeTimeout.request_id`, and in this
    message.
    """
    return ComfyError(
        f"the cancel for request {request_id} did not take effect: the request is still {status!r}",
        code=_CANCEL_NOT_APPLIED_CODE,
    )


def _subscribe_timed_out(
    handle: _RequestHandleBase,
    timed_out: BaseException,
    *,
    cancelled: bool,
    cancel_error: BaseException | None = None,
) -> SubscribeTimeout:
    """``timed_out`` re-expressed with the outcome of the cleanup cancel."""
    note = (
        "the request was cancelled"
        if cancelled
        else f"the cleanup cancel failed ({cancel_error}), so the request may still be running"
    )
    return SubscribeTimeout(
        f"{timed_out}; {note}",
        request_id=handle.request_id,
        model=handle.model,
        cancelled=cancelled,
        cancel_error=cancel_error,
    )


def _bounded(policy: RetryPolicy, budget: float | None) -> RetryPolicy:
    """``policy`` with both of its elapsed budgets capped at ``budget`` seconds.

    How a caller's deadline reaches the retrier: a poll made with two seconds
    left must not be allowed a minute of retries, and one made with nothing
    left gets exactly one attempt (a zero ``max_elapsed`` is ``NO_RETRY``).
    """
    if budget is None:
        return policy
    left = max(budget, 0.0)
    return replace(
        policy,
        max_elapsed=min(policy.max_elapsed, left),
        collect_max_elapsed=min(policy.collect_max_elapsed, left),
    )


def _http_timeout(budget: float | None) -> dict[str, Any]:
    """Keyword arguments bounding one low-level call's HTTP timeout by ``budget``.

    Empty when there is no budget, so the client's own timeout applies. Floored
    at :data:`_MIN_HTTP_TIMEOUT` for the reason given there.
    """
    if budget is None:
        return {}
    return {"timeout": max(budget, _MIN_HTTP_TIMEOUT)}


class _RequestHandleBase:
    """State and read-only views shared by the sync and async handles."""

    _model: str
    _request_id: str
    _retry: RetryPolicy

    @property
    def model(self) -> str:
        """The canonical ``{provider}/{model}`` id this request was submitted to.

        Part of the handle's identity rather than a convenience: every route in
        this family is addressed by the model id *and* the request id, which is
        why ``client.models.handle`` takes both.
        """
        return self._model

    @property
    def request_id(self) -> str:
        """The server-minted id for this request — all a rehydration needs."""
        return self._request_id

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, request_id={self._request_id!r})"


class RequestHandle(_RequestHandleBase):
    """A queued model request on :class:`~comfy_sdk.client.Comfy`.

    Built by ``client.models.submit`` and by ``client.models.handle``; there is
    nothing to construct by hand, and nothing in it that a second process
    cannot rebuild from :attr:`model` and :attr:`request_id`.
    """

    def __init__(
        self,
        low: ComfyLow,
        model: str,
        request_id: str,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        self._low = low
        self._model = model
        self._request_id = request_id
        self._retry = retry

    # -- polling (authoritative) ------------------------------------------
    def status(self) -> QueueUpdate:
        """Poll the queue once and return what it said.

        One request, no waiting — the queued surface's counterpart to
        :meth:`comfy_sdk.jobs.Job.refresh`. It reports a completion carrying an
        ``error_type`` as data on :attr:`QueueUpdate.error_type` rather than
        raising: this is the read a caller uses to *look*, and the raising
        belongs to :meth:`get`, which is the one that hands back a result.
        """
        return self._status(budget=None)

    def _status(self, *, budget: float | None, policy: RetryPolicy | None = None) -> QueueUpdate:
        """One authoritative poll, bounded by ``budget`` seconds when one is given.

        The bound covers the whole call — the HTTP request and any retry of it
        — so a caller's ``timeout`` on :meth:`iter_events` is a bound on the
        loop and not only on the pauses between its polls. ``policy``
        substitutes another retry policy outright, the way it does on
        :meth:`_call`; the poll loop never passes one, and the confirming poll
        in :meth:`_after_subscribe_timeout` passes ``NO_RETRY`` because it runs
        after the caller's deadline has already run out.
        """
        with translating():
            payload, headers = self._call(
                lambda: self._low.get_model_request_status(
                    self._model, self._request_id, **_http_timeout(budget)
                ),
                budget=budget,
                policy=policy,
            )
        return _update_from(payload, headers, request_id=self._request_id, require_status=True)

    def iter_events(self, timeout: float | None = None) -> Generator[QueueUpdate, None, None]:
        """Poll to completion, yielding an update whenever the queue moves.

        The first observation is always yielded; after that only a change in
        status or queue position is. The final yield is the completion itself,
        after which the iterator stops — it does *not* raise for a completion
        carrying an ``error_type``, because this is a view of the queue's
        progress and a caller who wants the result calls :meth:`get`, which
        does raise.

        ``timeout`` is a client-side bound in seconds on the whole loop — the
        polls, their retries and the pauses between them, not the pauses alone
        — and ``None`` polls until the server says the request is done. The
        first poll is always made, so ``timeout=0`` reads "look once". Past the
        bound no further poll is started, and the last one is held to what is
        left of it (with a floor of :data:`_MIN_HTTP_TIMEOUT` so it can still
        complete a handshake), so the loop overruns its bound by at most one
        such request. Exceeding it raises ``TimeoutError`` and leaves the
        request running — the queue is the server's, so a local clock running
        out says nothing about it. Cancelling on the way out is
        ``models.subscribe``'s behaviour, deliberately not this one's: an
        iterator that cancelled the work it was iterating would make a ``for``
        loop with a ``break`` destructive.
        """
        deadline = None if timeout is None else _now() + timeout
        backoff = _core.backoff_schedule()
        previous: QueueUpdate | None = None
        while True:
            remaining = _remaining(deadline)
            if previous is not None and remaining is not None and remaining <= 0:
                raise _timed_out(self._request_id, timeout, previous)
            update = self._status(budget=remaining)
            if _changed(previous, update):
                yield update
            previous = update
            if update.is_completed:
                return
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                raise _timed_out(self._request_id, timeout, update)
            delay = _pace(update, backoff)
            time.sleep(delay if remaining is None else min(delay, remaining))

    def get(self, timeout: float | None = None) -> dict[str, Any]:
        """Wait for the request to complete and return the provider's payload.

        The result is the partner model's own output, decoded from JSON and
        handed back as-is — the same value ``models.run`` returns for the same
        model and arguments, under the same ``dict[str, Any]`` annotation. That
        annotation is the contract: every model Router serves answers with a
        JSON object. A partner whose native output were an array or a bare
        value would still be handed back unchanged rather than rejected, since
        the payload is the partner's and not this SDK's to reshape — but that
        is robustness against an off-contract payload, not a second supported
        return type.

        Raises the typed router exception
        (:mod:`comfy_sdk.router_exceptions`) when the completion carries an
        ``error_type``, which is how the server reports a failed *or* cancelled
        request. ``timeout`` bounds the wait exactly as it does on
        :meth:`iter_events`, the result fetch included, and raises
        ``TimeoutError`` without cancelling.

        Calling it on a request that has already completed is one status poll
        and one fetch, so collecting a result twice — or from a second process
        — costs no more than the first time.
        """
        deadline = None if timeout is None else _now() + timeout
        completion = _last(self.iter_events(timeout=timeout))
        return self._collect(completion, budget=_remaining(deadline))

    def _collect(self, completion: QueueUpdate, *, budget: float | None = None) -> dict[str, Any]:
        """Turn an observed completion into a result, or into the typed error.

        Split out of :meth:`get` so ``models.subscribe`` — which has already
        polled its way to the completion — can collect from the update it is
        holding instead of spending one more status request re-discovering it.
        ``budget`` is what is left of the caller's deadline, and bounds the
        fetch the way :meth:`_status` bounds a poll.
        """
        _raise_for_completion(completion.raw, request_id=self._request_id)
        with translating():
            payload, _headers = self._call(
                lambda: self._low.get_model_request_result(
                    self._model, self._request_id, **_http_timeout(budget)
                ),
                budget=budget,
            )
        # Checked again on the result body: which of the two responses carries
        # the `error_type` is the server's choice, and reading only one of them
        # is how a failure gets returned as a result.
        _raise_for_completion(payload, request_id=self._request_id, envelope_only=True)
        return payload

    def cancel(self) -> QueueUpdate:
        """Ask the server to cancel this request.

        A request, not a guarantee — exactly as it is for a workflow job. A
        request that has already completed stays completed, so read the returned
        update's :attr:`~QueueUpdate.status`, or poll :meth:`status`, rather
        than assuming the work stopped. A deployment that answers with no body
        gives an update whose ``status`` is ``""``; the authoritative state is
        the next :meth:`status`.

        The route takes a request in **either** live state, answering ``202``
        with :data:`_CANCELLATION_REQUESTED`; it is only the terminal one it
        refuses, with a ``409`` that reaches you as
        :class:`~comfy_sdk.router_exceptions.AlreadyCompleted` — an exception,
        not an update. What "taken" does not settle is whether the generation
        stopped: one already on the wire at a partner may complete anyway, and
        a partner generation that completes is **billed** whether or not
        anyone collected it. ``models.subscribe``'s timeout confirms the
        difference with a poll; here that is the caller's to do, since an
        explicit ``cancel()`` is not a timeout cleaning up after itself.
        """
        with translating():
            payload, headers = self._call(
                lambda: self._low.put_model_request_cancel(self._model, self._request_id)
            )
        return _update_from(payload, headers, request_id=self._request_id)

    def _cancel_best_effort(self) -> QueueUpdate:
        """The cleanup cancel ``models.subscribe`` issues after its own timeout.

        One attempt under :data:`~comfy_sdk.retry.NO_RETRY` and a short HTTP
        bound, because it runs inside the handling of a ``TimeoutError`` the
        caller is about to see: a cancel that rode the client's full retry
        policy could hold that caller for the whole of ``max_elapsed`` — or
        ``collect_max_elapsed``, if the cancel were answered with a paced
        ``429`` — after they had already stopped waiting.

        Returns the update the cancel was answered WITH, rather than discarding
        it: a 2xx says the route accepted the message, not that the run
        stopped, and :meth:`_after_accepted_cancel` is what reads the
        difference. :meth:`cancel` says the same thing to its own callers.
        """
        with translating():
            payload, headers = self._call(
                lambda: self._low.put_model_request_cancel(
                    self._model, self._request_id, timeout=_CANCEL_TIMEOUT
                ),
                policy=NO_RETRY,
            )
        return _update_from(payload, headers, request_id=self._request_id)

    def _after_subscribe_timeout(self) -> DetachedRequest | QueueUpdate | None:
        """``models.subscribe``'s timeout teardown: cancel, then say what happened.

        Three answers, because the cancel has three meaningfully different
        outcomes and a caller who has just lost their wait needs to tell them
        apart:

        * ``None`` — the run is gone. Either the cancel's own body came back
          terminal, or the contract's ``202`` was confirmed by a poll that
          found the row ``COMPLETED``/``cancelled``; there is nothing to
          collect, so ``subscribe`` raises, exactly as it always has.
        * a :class:`DetachedRequest` — the run did NOT stop, and a confirming
          poll found it still going. Either the queue REFUSED the cancel on
          the request's own state (:func:`_refused_on_state`), or it took the
          cancel and the poll still found a live status. It is in flight, so
          it is served and billed whatever the caller does; the honest report
          is that the caller detached from a run that is still theirs to
          collect.
        * a :class:`QueueUpdate` — the request is ``COMPLETED``, found either
          by the confirming poll or on the cancel's own answer. It finished
          while the teardown was running, so there IS a result, and
          ``subscribe`` collects it rather than throwing away a generation the
          caller has been billed for.

        A 2xx is deliberately NOT read as proof the run stopped; see
        :func:`_reading_of_accepted_cancel` for what the body has to say
        before this reports a cancellation. The contract's own ``202``
        (:attr:`~_CancelReading.ACCEPTED`) falls through to
        :meth:`_detach_report` exactly as :attr:`~_CancelReading.UNSTOPPED`
        does — one poll settles which of the three endings it is, because the
        ``202`` body says the ask landed and nothing else.

        Any other cancel failure propagates. That is the narrowing this method
        exists for: the timeout path used to swallow every cancel failure
        alike, so a rejected credential or an unreachable server read exactly
        like a successful cancel and left the caller believing a still-running
        generation had been stopped.
        """
        try:
            accepted: QueueUpdate | None = self._cancel_best_effort()
        except _CANCEL_FAILURES as exc:
            if not _refused_on_state(exc):
                raise
            # Refused on state: fall through to the confirming poll below,
            # OUTSIDE this handler, so a failure there is not reported as
            # having happened "during handling of" the refusal.
            accepted = None
        if accepted is not None:
            reading = _reading_of_accepted_cancel(accepted)
            if reading is _CancelReading.STOPPED:
                return None
            if reading is _CancelReading.FINISHED:
                return accepted
            # ACCEPTED and UNSTOPPED both fall through: neither says what the
            # request's state IS, which is the one thing that decides the
            # ending, so both are settled by the same confirming poll.
        return self._detach_report()

    def _detach_report(self) -> DetachedRequest | QueueUpdate | None:
        """What one confirming poll found after a cancel that was not terminal.

        Reached three ways: the route ACCEPTED the ask with the contract's
        ``202`` (:attr:`~_CancelReading.ACCEPTED`), it REFUSED the cancel on
        the request's state (:func:`_refused_on_state`), or it accepted the
        message and echoed a live status
        (:attr:`~_CancelReading.UNSTOPPED`). None of the three says what the
        request's state actually is, so this reads it.

        One unretried, short-bounded poll: the caller's deadline has already
        run out, so this is not the place to spend a retry budget. The
        authoritative state of a request is always the next status read, never
        what a cancel answered.

        Four answers, from that read:

        * terminal and carrying the ``cancelled`` bucket
          (:func:`_is_cancelled_completion`) — ``None``, the cancelled ending.
          This is what the contract's ``202`` normally resolves to, because the
          route writes ``COMPLETED``/``cancelled`` before it answers. It is
          deliberately NOT routed through :meth:`_collect_or_detach`, whose
          ``error_type is not None`` branch re-raises the typed error: that is
          right for a run that failed on its own and wrong for a stop this SDK
          itself asked for.
        * terminal for any other reason, or none at all — the update, which
          ``subscribe`` collects (or raises the run's own typed failure from).
        * still :data:`IN_QUEUE` — a ``ComfyError`` coded
          :data:`_CANCEL_NOT_APPLIED_CODE`. The route's guard covers
          ``IN_QUEUE``, so a request still sitting there after an accepted
          cancel means the ask did not land, and the contract says such a
          request "was never dispatched and cannot be charged" — which is the
          one claim a :class:`DetachedRequest` must never make. ``subscribe``'s
          caller turns it into ``SubscribeTimeout(cancelled=False,
          cancel_error=...)``.
        * any other live status — :data:`IN_PROGRESS`, or a value the closed
          enum does not name — a detach, as before.

        A poll that fails does not undo what the cancel's answer established:
        the request was NOT confirmed stopped. So it still reports a detach,
        with :attr:`DetachedRequest.status` left empty to say the state was not
        confirmed, rather than raising and stranding the caller without the
        handle to the run they may now be paying for. An empty ``status`` is
        therefore the one value that leaves the billing claim UNVERIFIED: the
        poll may have failed on a rejected credential or a ``404``, and this
        does not tell those apart from a transient blip.
        """
        try:
            update = self._status(budget=_CANCEL_TIMEOUT, policy=NO_RETRY)
        except _CANCEL_FAILURES:
            return self._detached("")
        if _is_cancelled_completion(update):
            return None
        if update.is_completed:
            return update
        if update.status != IN_QUEUE:
            return self._detached(update.status)
        # OUTSIDE the handler above, so a cancel that never landed is not
        # chained onto whatever the poll happened to fail with first.
        raise _cancel_not_applied(self._request_id, update.status)

    def _collect_or_detach(
        self, completion: QueueUpdate, *, budget: float | None = None
    ) -> dict[str, Any] | DetachedRequest:
        """Collect a completion the timeout teardown found, or detach from it.

        The collect sits after the caller's deadline has already run out, and
        it can fail on its own — a transport error, a ``5xx``, or the bounded
        budget expiring. Letting that failure out raw is the stranding the
        detach report exists to prevent: the caller's ``except TimeoutError``
        never fires, and nothing hands back the ``request_id`` or the handle
        for a generation that HAS finished and HAS been billed. So a failed
        fetch degrades to a :class:`DetachedRequest` over the same handle,
        which the caller can collect from whenever they like.

        One failure is NOT degraded: a completion that carries its own
        ``error_type``. That is the run's outcome rather than a failure to
        read it, and the typed error is the honest answer — reporting a detach
        there would claim a run that ended is still going and still billing.
        """
        try:
            return self._collect(completion, budget=budget)
        except _CANCEL_FAILURES:
            if completion.error_type is not None:
                raise
            return self._detached(completion.status)

    def _detached(self, status: str) -> DetachedRequest:
        """This handle as the detach report ``models.subscribe`` hands back."""
        return DetachedRequest(
            request_id=self._request_id, model=self._model, status=status, handle=self
        )

    def _call(
        self,
        send: Callable[[], tuple[dict[str, Any], httpx.Headers]],
        *,
        budget: float | None = None,
        policy: RetryPolicy | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        """Run one queue call under the client's retry policy.

        The same ``Retrier`` ``models.run`` uses, constructed per call because
        its budget runs from its construction: a poll loop that shared one
        would spend the whole budget on its first hour of polling and then
        surface the next blip as a hard failure. What it buys here is that a
        ``429`` naming a ``Retry-After`` paces the poll instead of ending it.
        ``budget`` caps that policy's elapsed budgets at what is left of the
        caller's deadline; ``policy`` substitutes another policy outright.
        """
        retrier = Retrier(_bounded(policy or self._retry, budget), now=_now)
        while True:
            try:
                return send()
            except _CANDIDATE_FAILURES as exc:
                delay = retrier.delay_before_retry(exc)
                if delay is None:
                    raise
                time.sleep(delay)


class AsyncRequestHandle(_RequestHandleBase):
    """A queued model request on :class:`~comfy_sdk.client.AsyncComfy` — mirrors
    :class:`RequestHandle`."""

    def __init__(
        self,
        low: AsyncComfyLow,
        model: str,
        request_id: str,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        self._low = low
        self._model = model
        self._request_id = request_id
        self._retry = retry

    async def status(self) -> QueueUpdate:
        """Awaitable :meth:`RequestHandle.status` — one authoritative poll."""
        return await self._status(budget=None)

    async def _status(
        self, *, budget: float | None, policy: RetryPolicy | None = None
    ) -> QueueUpdate:
        """Async :meth:`RequestHandle._status` — one poll, bounded by ``budget``."""
        with translating():
            payload, headers = await self._call(
                lambda: self._low.get_model_request_status(
                    self._model, self._request_id, **_http_timeout(budget)
                ),
                budget=budget,
                policy=policy,
            )
        return _update_from(payload, headers, request_id=self._request_id, require_status=True)

    async def iter_events(self, timeout: float | None = None) -> AsyncGenerator[QueueUpdate, None]:
        """Async :meth:`RequestHandle.iter_events` — ``async for`` over the updates."""
        deadline = None if timeout is None else _now() + timeout
        backoff = _core.backoff_schedule()
        previous: QueueUpdate | None = None
        while True:
            remaining = _remaining(deadline)
            if previous is not None and remaining is not None and remaining <= 0:
                raise _timed_out(self._request_id, timeout, previous)
            update = await self._status(budget=remaining)
            if _changed(previous, update):
                yield update
            previous = update
            if update.is_completed:
                return
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                raise _timed_out(self._request_id, timeout, update)
            delay = _pace(update, backoff)
            await asyncio.sleep(delay if remaining is None else min(delay, remaining))

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        """Async :meth:`RequestHandle.get` — wait, then collect or raise."""
        deadline = None if timeout is None else _now() + timeout
        completion: QueueUpdate | None = None
        async for update in self.iter_events(timeout=timeout):
            completion = update
        return await self._collect(_completed(completion), budget=_remaining(deadline))

    async def _collect(
        self, completion: QueueUpdate, *, budget: float | None = None
    ) -> dict[str, Any]:
        """Async :meth:`RequestHandle._collect`."""
        _raise_for_completion(completion.raw, request_id=self._request_id)
        with translating():
            payload, _headers = await self._call(
                lambda: self._low.get_model_request_result(
                    self._model, self._request_id, **_http_timeout(budget)
                ),
                budget=budget,
            )
        _raise_for_completion(payload, request_id=self._request_id, envelope_only=True)
        return payload

    async def cancel(self) -> QueueUpdate:
        """Async :meth:`RequestHandle.cancel` — a request, not a guarantee."""
        with translating():
            payload, headers = await self._call(
                lambda: self._low.put_model_request_cancel(self._model, self._request_id)
            )
        return _update_from(payload, headers, request_id=self._request_id)

    async def _cancel_best_effort(self) -> QueueUpdate:
        """Async :meth:`RequestHandle._cancel_best_effort` — one bounded attempt."""
        with translating():
            payload, headers = await self._call(
                lambda: self._low.put_model_request_cancel(
                    self._model, self._request_id, timeout=_CANCEL_TIMEOUT
                ),
                policy=NO_RETRY,
            )
        return _update_from(payload, headers, request_id=self._request_id)

    async def _after_subscribe_timeout(self) -> AsyncDetachedRequest | QueueUpdate | None:
        """Async :meth:`RequestHandle._after_subscribe_timeout` — same three answers."""
        try:
            accepted: QueueUpdate | None = await self._cancel_best_effort()
        except _CANCEL_FAILURES as exc:
            if not _refused_on_state(exc):
                raise
            accepted = None
        if accepted is not None:
            reading = _reading_of_accepted_cancel(accepted)
            if reading is _CancelReading.STOPPED:
                return None
            if reading is _CancelReading.FINISHED:
                return accepted
        return await self._detach_report()

    async def _detach_report(self) -> AsyncDetachedRequest | QueueUpdate | None:
        """Async :meth:`RequestHandle._detach_report` — the same four answers."""
        try:
            update = await self._status(budget=_CANCEL_TIMEOUT, policy=NO_RETRY)
        except _CANCEL_FAILURES:
            return self._detached("")
        if _is_cancelled_completion(update):
            return None
        if update.is_completed:
            return update
        if update.status != IN_QUEUE:
            return self._detached(update.status)
        raise _cancel_not_applied(self._request_id, update.status)

    async def _collect_or_detach(
        self, completion: QueueUpdate, *, budget: float | None = None
    ) -> dict[str, Any] | AsyncDetachedRequest:
        """Async :meth:`RequestHandle._collect_or_detach`."""
        try:
            return await self._collect(completion, budget=budget)
        except _CANCEL_FAILURES:
            if completion.error_type is not None:
                raise
            return self._detached(completion.status)

    def _detached(self, status: str) -> AsyncDetachedRequest:
        """Async :meth:`RequestHandle._detached`."""
        return AsyncDetachedRequest(
            request_id=self._request_id, model=self._model, status=status, handle=self
        )

    async def _call(
        self,
        send: Callable[[], Awaitable[tuple[dict[str, Any], httpx.Headers]]],
        *,
        budget: float | None = None,
        policy: RetryPolicy | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        """Async :meth:`RequestHandle._call` — one queue call under the retry policy."""
        retrier = Retrier(_bounded(policy or self._retry, budget), now=_now)
        while True:
            try:
                return await send()
            except _CANDIDATE_FAILURES as exc:
                delay = retrier.delay_before_retry(exc)
                if delay is None:
                    raise
                await asyncio.sleep(delay)


@dataclass(frozen=True)
class _DetachedBase:
    """The shared body of the two detach reports."""

    #: The request that is STILL RUNNING — the same id
    #: ``client.models.handle`` rehydrates from, and the one to quote in a
    #: support request about the charge.
    request_id: str
    #: The canonical ``{provider}/{model}`` id it was submitted to. Needed
    #: alongside :attr:`request_id` because every route in this family is
    #: addressed by both.
    model: str
    #: The status the confirming poll reported for the request, verbatim —
    #: :data:`IN_PROGRESS` on the ordinary detach. It is one of the contract's
    #: **live** states, or a live value its closed enum does not name yet;
    #: :data:`COMPLETED` never reaches here (a terminal poll is collected or
    #: raised instead), and neither does :data:`IN_QUEUE`, which is the one
    #: state the contract pins as never dispatched and unchargeable — a detach
    #: asserts the opposite, so that combination is refused outright below.
    #:
    #: ``""`` when the poll itself failed, which says the state was not
    #: confirmed, never that the request stopped. That empty value is the one
    #: case where this report's "still running, and still billing" reading is
    #: UNVERIFIED: the poll is allowed to fail for any reason and they are not
    #: told apart — a rejected credential or a ``404`` saying the request is
    #: gone reads the same as a transient blip. Re-read
    #: :meth:`RequestHandle.status` before acting on the charge.
    status: str

    def __post_init__(self) -> None:
        """Refuse the one status a detach report cannot honestly carry.

        A detach says "this run is in flight, and you are being billed for
        it". :data:`IN_QUEUE` says the exact opposite — the contract's
        ``cancelled`` meaning pins a request cancelled in that state as never
        dispatched and unchargeable — so a report carrying it would be a
        billing claim that contradicts the spec. Every producer in this module
        already routes that case elsewhere; this is the invariant stated where
        it cannot be bypassed by a new one.
        """
        if self.status == IN_QUEUE:
            raise ValueError(
                f"a detach report cannot carry {IN_QUEUE!r}: a request in that state "
                "was never dispatched and cannot be charged"
            )


@dataclass(frozen=True)
class DetachedRequest(_DetachedBase):
    """What ``models.subscribe`` RETURNS when its timeout could not stop the run.

    The cancel route takes a request in either live state, but taking it is not
    the same as the run stopping: a partner generation already on the wire may
    complete anyway, and one that completes is **billed** whether or not
    anyone collected it. So when the confirming poll finds the request still
    running, the timeout is a *detach* and not a cancellation: the caller has
    stopped waiting, and the generation carries on without them.

    It is returned rather than raised because nothing has gone wrong — the run
    is healthy, it is the caller's patience that ran out — and because the
    generation is still theirs to collect::

        outcome = client.models.subscribe(model, arguments, timeout=30)
        if isinstance(outcome, DetachedRequest):
            print("still running, and still billed:", outcome.request_id)
            result = outcome.handle.get()      # ...or collect it later, elsewhere
        else:
            result = outcome

    Branch on the class, never on a message: that is the whole point of the
    type. The three ways a ``subscribe`` timeout can end are this, a
    :class:`SubscribeTimeout` (``cancelled=True`` when the row came back
    ``COMPLETED``/``cancelled`` — it really is gone; ``cancelled=False`` with
    a :attr:`~SubscribeTimeout.cancel_error` when the cancel did not land), and
    an ordinary result (it completed while the timeout was being torn down).
    """

    #: The live request, ready to poll or collect. The same object
    #: ``client.models.handle(model, request_id)`` rebuilds in any other
    #: process, so nothing here is required to stay in this one.
    handle: RequestHandle


@dataclass(frozen=True)
class AsyncDetachedRequest(_DetachedBase):
    """Awaitable-surface :class:`DetachedRequest` — what ``AsyncModels.subscribe`` returns."""

    #: The live request as an :class:`AsyncRequestHandle`.
    handle: AsyncRequestHandle


__all__ = [
    "COMPLETED",
    "IN_PROGRESS",
    "IN_QUEUE",
    "AsyncDetachedRequest",
    "AsyncRequestHandle",
    "DetachedRequest",
    "QueueUpdate",
    "RequestHandle",
    "SubscribeTimeout",
]
