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

**A retry never begins while the original attempt might still be running on the
server.** The client cannot observe the server's side of an aborted request, so
this is enforced structurally rather than by guessing:

1. Attempts are strictly sequential. One attempt's connection is closed and its
   backoff has elapsed before the next is opened; two attempts of one logical
   call are never in flight at once.
2. The retry deadline never interrupts an attempt. It is checked only *between*
   attempts, so the SDK never abandons a request that is merely slow — the
   per-attempt ``timeout`` is the only thing that ends an attempt, and on a run
   that is the long, generation-sized timeout rather than an ordinary API one.
3. A failure that leaves the request's fate *unknown* — no response arrived, so
   the server may still be generating — is not retried by default. Only
   failures where this attempt provably finished are: a completed 5xx response,
   or a connect-phase failure that never delivered the request at all. Opting
   in is :attr:`RetryPolicy.retry_possibly_in_flight`, and it is safe once the
   server replays a repeated ``Idempotency-Key`` rather than re-running it.

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

import random
from collections.abc import Callable
from dataclasses import dataclass

import httpx

#: Transport failures where the request was never delivered, so the server
#: cannot be working on it: the connection was never established, never taken
#: from the pool, or was refused by a proxy. Retrying one of these cannot
#: duplicate work. Checked before :data:`_POSSIBLY_IN_FLIGHT`, which some of
#: them also match (``ConnectError`` is a ``NetworkError``, ``ConnectTimeout``
#: a ``TimeoutException``).
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


def is_retryable_status(status: int) -> bool:
    """Whether an HTTP status is worth another attempt.

    5xx only. A 4xx is the server's considered answer about *this* request and
    repeating it spends money to be refused again — ``404`` (no such model),
    ``409``, ``422`` (invalid input) and a content-policy refusal are all
    deterministic, and none of them become true on the second ask. ``429`` is
    excluded too: it asks for a specific ``Retry-After`` pace rather than the
    blind backoff here, and the workflow surface already honours it separately.

    A ``502``/``504`` from an intermediary is included even though the origin
    behind it may still be generating — the response completed, so *this*
    attempt is definitively over. What keeps that retry from being billed twice
    is the unchanged ``Idempotency-Key``, not the status.
    """
    return 500 <= status <= 599


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
    #: imply an ever-lengthening final wait.
    max_backoff: float = 15.0
    #: Randomise each delay over ``[0, ceiling]`` ("full jitter") instead of
    #: waiting the ceiling exactly. On by default: identical clients that fail
    #: together would otherwise retry together, converting one outage into a
    #: synchronised thundering herd on the recovering server.
    jitter: bool = True
    #: Also retry when the request's fate is unknown — no response arrived, so
    #: the server may still be generating. Off by default: that retry is only
    #: safe against a server that replays a repeated ``Idempotency-Key``
    #: instead of running the request again. Turning it on usually means
    #: raising ``max_elapsed`` as well, since one full-length client timeout on
    #: a run can exhaust the default budget on its own.
    retry_possibly_in_flight: bool = False

    def __post_init__(self) -> None:
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
        be executing server-side.
        """
        status = getattr(exc, "http_status", None)
        if isinstance(status, int):
            return is_retryable_status(status)
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
        # Clamped so a pathological policy (tiny backoff, huge budget) cannot
        # reach an exponent that overflows the float before the cap applies.
        exponent = min(max(attempt - 1, 0), 64)
        ceiling = min(self.max_backoff, self.initial_backoff * self.backoff_factor**exponent)
        return ceiling * rng() if self.jitter else ceiling


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
        retryable, or the next attempt would start after the deadline. The
        deadline is checked against the *end* of the sleep, so no attempt ever
        begins outside the budget — the bound holds even when the backoff is
        long relative to what is left.
        """
        failed_at = self._now()
        self._attempts += 1
        if not self._policy.enabled or not self._policy.should_retry(exc):
            return None
        delay = self._policy.backoff(self._attempts, rng=self._rng)
        if failed_at + delay >= self._deadline:
            return None
        return delay


__all__ = [
    "DEFAULT_RETRY",
    "NO_RETRY",
    "Retrier",
    "RetryPolicy",
    "is_retryable_status",
]
