"""Bounded, idempotent client-side retry for ``client.models.run``.

A model run holds one HTTP connection open for as long as the generation takes
— minutes, for a provider the platform submits to and polls internally. That
shape is what makes a naive retry expensive rather than merely noisy: the
client gives up on a connection, retries, and a *second* generation is started
and billed for one logical request.

Two properties of this module exist to make that impossible.

**Every attempt of one logical call sends the same ``Idempotency-Key``.** The
key is minted once, before the first attempt, and reused verbatim on each
retry; a *new* call mints a new key. That is what lets a server recognise a
retry as the same request rather than a second order. The key is sent
unconditionally — there is no configuration that turns it off — because a retry
without it is exactly the double-charge above.

**Only failures the key survives are retried by default.** Every documented
statement this repo makes about ``Idempotency-Key`` says it is *single-use,
reject-on-duplicate, with no response replay* — ``spec/openapi.yaml``'s shared
``IdempotencyKey`` parameter, :class:`~comfy_sdk.exceptions.IdempotencyKeyReuse`
and the README all agree that a later request presenting the same key is
rejected ``422`` ``idempotency_key_reuse`` rather than re-run or replayed. The
spec is equally explicit about when a key is *released* instead of claimed: a
request that "definitively fails without creating a job (a validation error, or
an upstream reject such as out-of-credits or queue-full)" frees it, while one
whose outcome the server cannot characterise ("an upstream timeout or 5xx where
the job may or may not have been created") keeps it claimed.

``POST /models/run`` is not itself in that spec, so its key semantics are
undocumented — which is the point. Retrying under one key is only safe against a
server that *replays* a claimed key, and nothing here says this route does; the
alternatives are a ``422`` that replaces the real error, or a second billed
generation. So the SDK retries by default only where the key is provably still
unspent, and leaves the rest to an explicit opt-in.

That, not a guess about the network, is what sorts the failures:

1. **Never delivered** — the connection was refused, timed out before it was
   established, or never came out of the pool. The request never reached
   submission, so the key was never claimed and the same key is still
   spendable. Retried by default; this is the one class where one-key retry is
   provably free.
2. **Rejected without starting work** — a ``429`` that names a pace in
   ``Retry-After``. The contract releases the key for exactly this (queue-full,
   out-of-credits, a concurrency limit) and tells clients to treat "429 +
   ``Retry-After``" as back-off-and-retry, so it is retried by default at the
   pace the server asked for rather than a blind backoff of our own.
3. **Outcome unknown** — a completed 5xx, or a transport failure that may have
   delivered the request in full (a read timeout on a run is the important
   member: the generation-sized client timeout expired with no answer, which is
   precisely when the server is most likely still generating). This is the
   class the spec keeps the key claimed for, so a same-key retry here is a
   ``422`` that replaces the real error — and a *fresh*-key retry is the second
   billed generation this module exists to prevent. Not retried by default.
   :attr:`RetryPolicy.retry_possibly_in_flight` opts in, and it is correct
   exactly when a deployment replays a repeated key instead of rejecting it.
4. **Everything else** — every other 4xx is the server's considered answer
   about *this* request, and asking again spends money to be refused again.
   Never retried.

**The router's ``service_unavailable`` is not retried by default.** The vendored
router contract tells a *caller* to "retry it with backoff: it is the one bucket
whose condition clears on its own", and that advice is sound — but it arrives on
a ``503``, and the question this module answers before retrying anything is not
"will the condition clear" but "is the one ``Idempotency-Key`` still spendable".
Neither vendored contract says a ``503`` releases the key; the v2 contract says
the opposite for the whole 5xx class ("an upstream timeout or 5xx where the job
may or may not have been created" keeps it claimed), and the router spec
documents a same-key retry for exactly one bucket, ``deadline_exceeded``, and is
silent about this one. Retrying it by default would therefore trade a
diagnosable ``503`` for a ``422 idempotency_key_reuse`` on every deployment that
rejects a repeated key. So it stays in class 3 above, where
:attr:`RetryPolicy.retry_possibly_in_flight` opts in — and that opt-in is the
route to the contract's advice, because it keeps the one key across the retry.

Catching :class:`~comfy_sdk.router_exceptions.ServiceUnavailable` and retrying
by hand is *not* the same thing, and the difference is billable: ``models.run()``
mints a **fresh** ``Idempotency-Key`` per call, so a bare ``run(...)`` again
after a ``503`` presents a new key for a request the server may already be
generating — the second billed generation the one-key rule exists to prevent. A
hand-written retry is only safe when it passes the *same* explicit
``idempotency_key=`` it used the first time **and** the deployment is documented
to replay a repeated key rather than reject it::

    import uuid
    from comfy_sdk.router_exceptions import ServiceUnavailable

    key = str(uuid.uuid4())
    try:
        result = client.models.run(model, args, idempotency_key=key)
    except ServiceUnavailable:
        result = client.models.run(model, args, idempotency_key=key)  # same key

Against a deployment that rejects a repeated key that retry comes back
``422 idempotency_key_reuse`` — which is the honest failure, not a double
charge. When in doubt, prefer ``RetryPolicy(retry_possibly_in_flight=True)``.
Revisit this the moment the router contract states what a ``503`` does to the
key.

**A retry never begins while the original attempt might still be running on the
server.** Beyond the classification above this is also enforced structurally:

* Attempts are strictly sequential. One attempt's connection is closed and its
  backoff has elapsed before the next is opened; two attempts of one logical
  call are never in flight at once.
* The retry deadline never interrupts an attempt. It is checked only *between*
  attempts, so the SDK never abandons a request that is merely slow — the
  per-attempt ``timeout`` is the only thing that ends an attempt, and on a run
  that is the long, generation-sized timeout rather than an ordinary API one.

The budget is **total elapsed time**, never an attempt count: a per-attempt
budget multiplies out, and N attempts each allowed their own timeout stack into
a wait far longer than anything anybody chose. ``max_elapsed`` bounds when the
*last* attempt may start, so the worst case is that bound plus one per-attempt
timeout, and the number of attempts falls out of the backoff schedule.

Retry is on by default. ``Comfy(retry=NO_RETRY)`` turns it off; any other
policy is a :class:`RetryPolicy` you construct::

    from comfy_sdk import Comfy, NO_RETRY, RetryPolicy

    Comfy(retry=NO_RETRY)                              # exactly one attempt
    Comfy(retry=RetryPolicy(max_elapsed=300.0))        # keep trying for 5 min
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass

import httpx

#: Transport failures where the request was never delivered, so the server
#: cannot be working on it and cannot have claimed the ``Idempotency-Key``:
#: the connection was never established, never taken from the pool, or was
#: refused by a proxy. Retrying one of these under the same key cannot
#: duplicate work and cannot collide with the key's earlier use. Checked before
#: :data:`_POSSIBLY_IN_FLIGHT`, which some of them also match
#: (``ConnectError`` is a ``NetworkError``, ``ConnectTimeout`` a
#: ``TimeoutException``).
_NEVER_DELIVERED: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
)

#: Transport failures that leave the request's fate unknown — it may have been
#: delivered in full and still be executing server-side. A read timeout on a
#: run is the important member: the generation-sized client timeout expired
#: with no answer, which is precisely the case where the server is most likely
#: still generating.
_POSSIBLY_IN_FLIGHT: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

#: The one 4xx another attempt can change, and only when it carries a pace.
_TOO_MANY_REQUESTS = 429

#: Policy fields that must be real numbers for the arithmetic below to mean
#: anything. Kept beside the fields themselves so a numeric one added later is
#: added here too.
_NUMERIC_FIELDS = ("max_elapsed", "initial_backoff", "backoff_factor", "max_backoff")


def is_unknown_outcome_status(status: int) -> bool:
    """Whether a completed response leaves this request's outcome *unknown*.

    5xx, and "unknown" is the operative word rather than "transient". The
    vendored contract keeps an ``Idempotency-Key`` claimed for a request whose
    outcome the server cannot characterise — "an upstream timeout or 5xx where
    the job may or may not have been created" — and rejects any later request
    presenting a claimed key ``422`` ``idempotency_key_reuse``. That is why
    this class sits behind :attr:`RetryPolicy.retry_possibly_in_flight` rather
    than being retried by default: unless the deployment replays a claimed key,
    the same-key retry cannot succeed and *replaces* the genuine 5xx with a
    confusing key-reuse error.

    A ``502``/``504`` from an intermediary belongs here for the same reason:
    the proxy's response completed, which says nothing about whether the origin
    behind it stopped generating.
    """
    return 500 <= status <= 599


def retry_after_of(exc: BaseException) -> float | None:
    """The pace the server named, in seconds, or ``None`` if it named none.

    Read off whatever exception carries it — the transport parses
    ``Retry-After`` into ``ApiError.retry_after`` for any status. A value that
    is not a usable number of seconds is treated as absent rather than trusted
    into the arithmetic below.
    """
    raw = getattr(exc, "retry_after", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    seconds = float(raw)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


@dataclass(frozen=True)
class RetryPolicy:
    """When and how often to retry one logical call.

    Immutable, so a policy can be shared by every call on a client without one
    call's state leaking into another's. Bounds are wall-clock, not attempt
    counts — see this module's docstring for why.
    """

    #: Seconds from the first attempt after which no *new* attempt is started.
    #: An attempt already running is never interrupted by it, so the worst-case
    #: wall clock for a call is this plus one per-attempt ``timeout``. Zero
    #: disables retrying entirely (see :data:`NO_RETRY`).
    max_elapsed: float = 60.0
    #: Ceiling on the delay before the first retry. With ``jitter`` on — the
    #: default — the actual delay is drawn from ``[0, this]``.
    initial_backoff: float = 0.5
    #: Multiplier applied to the backoff ceiling after each attempt.
    backoff_factor: float = 2.0
    #: Ceiling the growing backoff holds at, so a long ``max_elapsed`` does not
    #: imply an ever-lengthening final wait. A ``Retry-After`` the server sent
    #: is honoured as given and is not capped by this — the point of a named
    #: pace is that the server knows better than the schedule here.
    max_backoff: float = 15.0
    #: Randomise each delay over ``[0, ceiling]`` ("full jitter") instead of
    #: waiting the ceiling exactly. On by default: identical clients that fail
    #: together would otherwise retry together, converting one outage into a
    #: synchronised thundering herd on the recovering server.
    jitter: bool = True
    #: Also retry when the request's outcome is unknown — a completed 5xx, or a
    #: transport failure that may have delivered the request, so the server may
    #: still be generating. Off by default, and the reason is the contract
    #: rather than caution: every documented statement about
    #: ``Idempotency-Key`` makes it single-use with no replay, and the spec
    #: keeps it *claimed* across exactly this class of failure — so a same-key
    #: retry here comes back ``422`` ``idempotency_key_reuse`` and hides the
    #: real error. Turn it on for a deployment that replays a repeated key
    #: instead of rejecting it — and raise ``max_elapsed`` when you do, since
    #: one full-length client timeout on a run can spend the default budget on
    #: its own.
    retry_possibly_in_flight: bool = False

    def __post_init__(self) -> None:
        for name in _NUMERIC_FIELDS:
            # NaN fails every ordered comparison below and infinity passes them
            # all, so both would construct cleanly and misbehave much later: an
            # infinite budget never reaches its deadline and retries a
            # permanently failing server forever, a NaN backoff makes the
            # caller's `sleep` raise `ValueError` in place of the real error,
            # and a NaN budget silently reads as "retrying disabled".
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be a finite number")
        if self.max_elapsed < 0:
            raise ValueError("max_elapsed must not be negative")
        if self.initial_backoff <= 0:
            raise ValueError("initial_backoff must be positive")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if self.max_backoff < self.initial_backoff:
            raise ValueError("max_backoff must be >= initial_backoff")

    @property
    def enabled(self) -> bool:
        """Whether this policy ever retries."""
        return self.max_elapsed > 0

    def should_retry(self, exc: BaseException) -> bool:
        """Whether ``exc`` is a condition another attempt could survive.

        An exception carrying an ``http_status`` is a response the server
        actually sent, whatever layer modelled it (the protocol ``ApiError``,
        an SDK exception, a router one) — so the status decides. Everything
        else is a transport failure, decided by whether the request can still
        be executing server-side. See this module's docstring for the four
        classes and why the key's contract, not the network, sorts them.
        """
        status = getattr(exc, "http_status", None)
        if isinstance(status, int):
            if status == _TOO_MANY_REQUESTS:
                # Rejected without starting work, so the contract released the
                # key and the same one is still spendable. Only with a pace
                # attached: the spec's retry signal is "429 + Retry-After",
                # and a 429 without one is not asking to be asked again.
                return retry_after_of(exc) is not None
            if is_unknown_outcome_status(status):
                # Every 5xx, including the router's `service_unavailable` 503:
                # the bucket says the condition clears on its own, but nothing
                # says the Idempotency-Key does. See this module's docstring.
                return self.retry_possibly_in_flight
            # Every other 4xx is deterministic — 404 (no such model), 409, 422
            # (invalid input), a content-policy refusal — and none of them
            # become true on the second ask.
            return False
        if isinstance(exc, _NEVER_DELIVERED):
            return True
        if isinstance(exc, _POSSIBLY_IN_FLIGHT):
            return self.retry_possibly_in_flight
        return False

    def backoff(self, attempt: int, *, rng: Callable[[], float] = random.random) -> float:
        """Seconds to wait before retry number ``attempt`` (1 is the first).

        ``rng`` is injectable so the schedule can be asserted exactly; callers
        outside the tests leave it alone.
        """
        ceiling = self._ceiling(max(attempt - 1, 0))
        return ceiling * rng() if self.jitter else ceiling

    def _ceiling(self, exponent: int) -> float:
        """The un-jittered delay ceiling ``exponent`` doublings in.

        The comparison against ``max_backoff`` happens *before* the
        exponentiation rather than after it. ``initial_backoff *
        backoff_factor**exponent`` is fully evaluated before any ``min()``
        could cap it, and CPython's float ``**`` raises ``OverflowError``
        rather than saturating to infinity — which would escape
        :meth:`Retrier.delay_before_retry` and replace the genuine API error
        with an arithmetic one. Deciding in log space also means no attempt
        count is too large to answer, where clamping the exponent instead
        would freeze a gentle ``backoff_factor`` short of its own ceiling.
        """
        if self.backoff_factor == 1.0 or self.initial_backoff >= self.max_backoff:
            return min(self.initial_backoff, self.max_backoff)
        reaches_ceiling_at = math.log(self.max_backoff / self.initial_backoff) / math.log(
            self.backoff_factor
        )
        if exponent >= reaches_ceiling_at:
            return self.max_backoff
        return min(self.max_backoff, self.initial_backoff * self.backoff_factor**exponent)


#: The policy every client uses unless told otherwise.
DEFAULT_RETRY = RetryPolicy()

#: Retrying turned off: one logical call is exactly one attempt. Pass it as
#: ``Comfy(retry=NO_RETRY)``. The ``Idempotency-Key`` is still sent — it is not
#: part of the retry policy, and a caller who retries by hand needs it.
NO_RETRY = RetryPolicy(max_elapsed=0.0)


class Retrier:
    """One logical call's place in a :class:`RetryPolicy`.

    Sans-IO: it decides *whether* and *how long* to wait, and the sync and
    async layers do the waiting (``time.sleep`` / ``asyncio.sleep``). Single
    use — construct one per logical call, immediately before the first
    attempt, since the budget is measured from its construction.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        now: Callable[[], float],
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._policy = policy
        self._now = now
        self._rng = rng
        self._attempts = 0
        # The whole call's budget, fixed here so every later decision measures
        # against one origin — elapsed time across all attempts, not time
        # granted afresh to each of them.
        self._deadline = now() + policy.max_elapsed

    @property
    def attempts(self) -> int:
        """Attempts that have failed so far."""
        return self._attempts

    def delay_before_retry(self, exc: BaseException) -> float | None:
        """Seconds to sleep before retrying after ``exc``, or ``None`` to give up.

        ``None`` means the caller re-raises: either the failure is not
        retryable, or the budget is already spent. A delay that would overshoot
        the deadline is *clamped* to what is left rather than treated as a
        refusal — with full jitter one unlucky draw would otherwise end a call
        that still had seconds of budget, making identical calls take a
        randomly varying number of attempts. ``client.py::_retry_delay`` bounds
        the workflow surface's 429 wait the same way. So ``max_elapsed`` is
        when the last attempt may *start*, inclusive.
        """
        failed_at = self._now()
        self._attempts += 1
        if not self._policy.enabled or not self._policy.should_retry(exc):
            return None
        remaining = self._deadline - failed_at
        if remaining <= 0:
            return None
        # A pace the server named beats the schedule guessed here.
        named = retry_after_of(exc)
        delay = named if named is not None else self._policy.backoff(self._attempts, rng=self._rng)
        return min(delay, remaining)


__all__ = [
    "DEFAULT_RETRY",
    "NO_RETRY",
    "Retrier",
    "RetryPolicy",
    "is_unknown_outcome_status",
    "retry_after_of",
]
