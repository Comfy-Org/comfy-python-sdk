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
from .router_exceptions import CancelRefused, RouterError, error_from_completion

#: The one terminal queue status. Deliberately a single value rather than a
#: :data:`comfy_sdk._core.TERMINAL`-style set: the server does not express a
#: cancel or a failure as its own status, it expresses them as this status plus
#: an ``error_type``. Adding "CANCELED" here on the assumption it exists would
#: strand a caller whose request really did reach ``COMPLETED``.
COMPLETED = "COMPLETED"

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
#: refuses it, rather than the caller, the credential or the server. The queue
#: honours a cancel only while the request is still waiting to be dispatched: a
#: run already in flight is served and billed, and one that has already finished
#: cannot be un-finished. Both refusals are the plain HTTP reading of ``409``
#: -- the target's current state conflicts with the operation -- and that
#: reading is what this keys on, deliberately rather than on the refusal's
#: prose, which no contract pins and which differs between the two.
#:
#: It is the FALLBACK reading, not the first one. Cancel-after-completion is
#: now typed -- it reaches this SDK as
#: :class:`~comfy_sdk.router_exceptions.AlreadyCompleted` under the
#: :class:`~comfy_sdk.router_exceptions.CancelRefused` base -- and
#: :func:`_refused_on_state` asks that question first, because branching on the
#: class is strictly better than branching on a status. This status is what
#: catches the refusal the contract has NOT named yet: the in-flight one, which
#: today carries no bucket on the header or in the body and so arrives as a
#: bare :class:`~comfy_sdk.exceptions.ComfyError`. It is matched only when the
#: response names no bucket at all, so a ``409`` the contract DOES name --
#: ``invalid_input``, ``concurrency_limit_exceeded`` -- is not swept in with
#: it. When the in-flight refusal is given a bucket of its own, add the
#: subclass to :data:`~comfy_sdk.router_exceptions.CANCEL_REFUSALS` and this
#: status clause can go.
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
        #: ``True`` only when the queue ACCEPTED the cleanup cancel, which it
        #: does only while the request is still waiting to be dispatched. The
        #: run is then gone and there is nothing left to collect or be billed
        #: for. ``False`` means the cancel failed and the run's fate is
        #: unknown -- read :attr:`cancel_error`, and treat the request as
        #: possibly still running.
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
      all** -- the shape the in-flight refusal has today, which carries no
      ``error_type`` on the header or in the body and so reaches this SDK as a
      bare :class:`~comfy_sdk.exceptions.ComfyError`.

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
    #: The route took the message but the run has NOT stopped -- a ``202``
    #: naming a ``CANCELING`` status, or a ``200`` on a request that won the
    #: race into flight. Confirmed against the server, then reported as a
    #: detach.
    UNSTOPPED = "unstopped"


def _reading_of_accepted_cancel(accepted: QueueUpdate) -> _CancelReading:
    """Read a 2xx cancel's own body for whether the work actually stopped.

    A 2xx on the cancel route says the server accepted the *message*, which is
    not the same as the run having stopped -- the binding accepts ``200``,
    ``202`` and ``204`` alike, and :meth:`cancel`'s docstring already tells its
    own callers to "read the returned update's status ... rather than assuming
    the work stopped". The timeout teardown used to discard that body and
    assert ``cancelled=True`` from the bare fact of a 2xx, which is the one
    reading the response does not support.

    Three readings, from the update the cancel answered with:

    * no status at all (the body-less ``204`` an accepted cancel usually is) --
      :attr:`~_CancelReading.STOPPED`. There is nothing to read, and an
      accepted cancel on an undispatched request is what that shape means;
      spending another round trip to re-confirm the ordinary case would charge
      every timeout for the rare one.
    * a terminal :data:`COMPLETED` carrying an ``error_type`` --
      :attr:`~_CancelReading.STOPPED` too. Terminal, and the bucket is how this
      queue expresses a stop (it has no ``CANCELED`` status of its own; see
      :data:`COMPLETED`).
    * a terminal :data:`COMPLETED` carrying NO bucket --
      :attr:`~_CancelReading.FINISHED`. It ran to completion and was billed, so
      there is a real result behind it.
    * anything else, which is a non-terminal status on a live request --
      :attr:`~_CancelReading.UNSTOPPED`.
    """
    if not accepted.status:
        return _CancelReading.STOPPED
    if not accepted.is_completed:
        return _CancelReading.UNSTOPPED
    return _CancelReading.STOPPED if accepted.error_type is not None else _CancelReading.FINISHED


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

        The queue honours a cancel only while the request is still waiting to
        be dispatched. A run it has already started is served to the end and
        **billed**, and asking to cancel it is *refused* rather than ignored —
        which reaches you as an exception, not as an update. ``models.subscribe``
        reads that refusal as a detach; here it is the caller's to handle, since
        an explicit ``cancel()`` is not a timeout cleaning up after itself.
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

        * ``None`` — the run is gone. Either the queue ACCEPTED the cancel on
          a request it had not dispatched yet, or the cancel's own body came
          back terminal; nothing will be billed further and there is nothing
          to collect, so ``subscribe`` raises, exactly as it always has.
        * a :class:`DetachedRequest` — the run did NOT stop, and a confirming
          poll found it still going. Either the queue REFUSED the cancel on
          the request's own state (:func:`_refused_on_state`), or it took the
          cancel but answered with a live status
          (:attr:`~_CancelReading.UNSTOPPED`). It was already in flight, so it
          is served and billed whatever the caller does; the honest report is
          that the caller detached from a run that is still theirs to collect.
        * a :class:`QueueUpdate` — the request is ``COMPLETED``, found either
          by the confirming poll or on the cancel's own answer. It finished
          while the teardown was running, so there IS a result, and
          ``subscribe`` collects it rather than throwing away a generation the
          caller has been billed for.

        A 2xx is deliberately NOT read as proof the run stopped; see
        :func:`_reading_of_accepted_cancel` for what the body has to say
        before this reports a cancellation.

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
        return self._detach_report()

    def _detach_report(self) -> DetachedRequest | QueueUpdate:
        """What a cancel that did not stop the run left behind, confirmed.

        Reached two ways, which establish the same thing: the queue REFUSED
        the cancel on the request's state (:func:`_refused_on_state`), or it
        accepted the message and answered with a live status
        (:attr:`~_CancelReading.UNSTOPPED`). Either way the run did not stop.

        One unretried, short-bounded poll: the caller's deadline has already
        run out, so this is not the place to spend a retry budget. The poll is
        what separates "still running" from "finished while we were tearing
        down", which neither answer says on its own — and the authoritative
        state of a request is always the next status read, never what a cancel
        answered.

        A poll that fails does not undo what the cancel's answer established:
        the request was NOT cancelled. So it still reports a detach, with
        :attr:`DetachedRequest.status` left empty to say the state was not
        confirmed, rather than raising and stranding the caller without the
        handle to the run they are now paying for. An empty ``status`` is
        therefore the one value that leaves the billing claim UNVERIFIED: the
        poll may have failed on a rejected credential or a ``404``, and this
        does not tell those apart from a transient blip.
        """
        try:
            update = self._status(budget=_CANCEL_TIMEOUT, policy=NO_RETRY)
        except _CANCEL_FAILURES:
            return self._detached("")
        if update.is_completed:
            return update
        return self._detached(update.status)

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

    async def _detach_report(self) -> AsyncDetachedRequest | QueueUpdate:
        """Async :meth:`RequestHandle._detach_report`."""
        try:
            update = await self._status(budget=_CANCEL_TIMEOUT, policy=NO_RETRY)
        except _CANCEL_FAILURES:
            return self._detached("")
        if update.is_completed:
            return update
        return self._detached(update.status)

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
    #: ``"IN_PROGRESS"`` on the ordinary detach. ``""`` when that poll itself
    #: failed, which says the state was not confirmed, never that the request
    #: stopped: the refused cancel had already established that it did not.
    #:
    #: An empty value is the one case where this report's "still running, and
    #: still billing" reading is UNVERIFIED. The poll behind it is allowed to
    #: fail for any reason and they are not told apart — a rejected credential
    #: or a ``404`` saying the request is gone reads the same as a transient
    #: blip. Re-read :meth:`RequestHandle.status` before acting on the charge.
    status: str


@dataclass(frozen=True)
class DetachedRequest(_DetachedBase):
    """What ``models.subscribe`` RETURNS when its timeout could not cancel the run.

    The queue honours a cancel only before it dispatches a request. Past that
    point the run is served and **billed** whatever the caller does, so a
    ``subscribe`` timeout is a *detach* and not a cancellation: the caller has
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
    :class:`SubscribeTimeout` with ``cancelled=True`` (the request was still
    queued and really is gone), and an ordinary result (it completed while the
    timeout was being torn down).
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
    "AsyncDetachedRequest",
    "AsyncRequestHandle",
    "DetachedRequest",
    "QueueUpdate",
    "RequestHandle",
    "SubscribeTimeout",
]
