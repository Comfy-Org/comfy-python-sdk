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
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from comfy_low.errors import ApiError
from comfy_low.transport import AsyncComfyLow, ComfyLow, parse_request_id

from . import _core
from .exceptions import ComfyError, translating
from .retry import DEFAULT_RETRY, NO_RETRY, Retrier, RetryPolicy
from .router_exceptions import RouterError, error_from_completion

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

    def _status(self, *, budget: float | None) -> QueueUpdate:
        """One authoritative poll, bounded by ``budget`` seconds when one is given.

        The bound covers the whole call — the HTTP request and any retry of it
        — so a caller's ``timeout`` on :meth:`iter_events` is a bound on the
        loop and not only on the pauses between its polls.
        """
        with translating():
            payload, headers = self._call(
                lambda: self._low.get_model_request_status(
                    self._model, self._request_id, **_http_timeout(budget)
                ),
                budget=budget,
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
        """
        with translating():
            payload, headers = self._call(
                lambda: self._low.put_model_request_cancel(self._model, self._request_id)
            )
        return _update_from(payload, headers, request_id=self._request_id)

    def _cancel_best_effort(self) -> None:
        """The cleanup cancel ``models.subscribe`` issues after its own timeout.

        One attempt under :data:`~comfy_sdk.retry.NO_RETRY` and a short HTTP
        bound, because it runs inside the handling of a ``TimeoutError`` the
        caller is about to see: a cancel that rode the client's full retry
        policy could hold that caller for the whole of ``max_elapsed`` — or
        ``collect_max_elapsed``, if the cancel were answered with a paced
        ``429`` — after they had already stopped waiting.
        """
        with translating():
            self._call(
                lambda: self._low.put_model_request_cancel(
                    self._model, self._request_id, timeout=_CANCEL_TIMEOUT
                ),
                policy=NO_RETRY,
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

    async def _status(self, *, budget: float | None) -> QueueUpdate:
        """Async :meth:`RequestHandle._status` — one poll, bounded by ``budget``."""
        with translating():
            payload, headers = await self._call(
                lambda: self._low.get_model_request_status(
                    self._model, self._request_id, **_http_timeout(budget)
                ),
                budget=budget,
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

    async def _cancel_best_effort(self) -> None:
        """Async :meth:`RequestHandle._cancel_best_effort` — one bounded attempt."""
        with translating():
            await self._call(
                lambda: self._low.put_model_request_cancel(
                    self._model, self._request_id, timeout=_CANCEL_TIMEOUT
                ),
                policy=NO_RETRY,
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


__all__ = ["COMPLETED", "AsyncRequestHandle", "QueueUpdate", "RequestHandle"]
