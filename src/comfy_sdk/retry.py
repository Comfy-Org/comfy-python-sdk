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

**Only failures the key survives are retried by default.** The v2 jobs contract
in ``spec/openapi.yaml`` makes its shared ``IdempotencyKey`` parameter
*single-use, reject-on-duplicate, with no response replay*: the first request to
present a key is processed and any later one presenting it is rejected ``422``
``idempotency_key_reuse`` rather than re-run or replayed (see
:class:`~comfy_sdk.exceptions.IdempotencyKeyReuse`). That spec is equally
explicit about when a key is *released* instead of claimed: a request that
"definitively fails without creating a job (a validation error, or an upstream
reject such as out-of-credits or queue-full)" frees it, while one whose outcome
the server cannot characterise ("an upstream timeout or 5xx where the job may or
may not have been created") keeps it claimed.

``POST /models/run`` is not itself in that spec, so its key semantics are not
that spec's to state — and for one failure the *router* contract states them
directly. ``spec/router-openapi.yaml``'s ``deadline_exceeded`` bucket says to
"retry it with the SAME ``Idempotency-Key``: when the provider had already
accepted the generation, the retry collects that generation rather than
dispatching another", and pins the ``Retry-After`` it carries to "seconds to
wait before retrying the SAME request with the SAME ``Idempotency-Key``". So
the sorting rule is unchanged — retry where the one key is documented to be
spendable again — and one class is added to it, for the case where the server
has already said so on the wire.

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
3. **Collectable** — the server answered that the work it already holds is not
   finished, *and* named the pace at which to ask the same key again for it: a
   router ``deadline_exceeded`` ``504`` carrying ``Retry-After``, and a ``409``
   carrying ``Retry-After`` ("still in progress") from the idempotency layer on
   the retry that follows it. This is the one class where the *server* has
   stated the same-key resend is safe, and the pace it names is its own poll
   interval — so it is retried by default, at that pace, and one ``run()`` call
   rides the collect loop to the finished generation instead of handing the
   caller a ``504`` for work that is still running.
   :func:`is_collectable` is the predicate and
   :attr:`RetryPolicy.retry_collectable` switches it off. Two gates keep it
   narrow: the ``504`` must name the ``deadline_exceeded`` bucket (a bucket-less
   ``504`` reads as ``provider_timeout``, where no contract blesses the resend),
   and the ``Retry-After`` must be there (the router sends it only when it holds
   a handle to a generation to collect — absent it, there is nothing to collect
   and the ``504`` falls back to class 4).
4. **Outcome unknown** — a completed 5xx that named no collectable pace, or a
   transport failure that may have delivered the request in full (a read timeout
   on a run is the important member: the generation-sized client timeout expired
   with no answer, which is precisely when the server is most likely still
   generating). Nothing on the wire says the key survives this, so a same-key
   retry may be a ``422`` that replaces the real error — and a *fresh*-key retry
   is the second billed generation this module exists to prevent. Not retried by
   default. :attr:`RetryPolicy.retry_possibly_in_flight` opts in, and it is
   correct exactly when a deployment replays a repeated key instead of rejecting
   it.
5. **Everything else** — every other 4xx is the server's considered answer
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
documents a same-key retry for exactly one bucket, ``deadline_exceeded`` — which
is why that one is class 3 above and this one is not. The spec is silent about
what a ``503`` does to the key. Retrying it by default would therefore trade a
diagnosable ``503`` for a ``422 idempotency_key_reuse`` on every deployment that
rejects a repeated key. So it stays in class 4 above, where
:attr:`RetryPolicy.retry_possibly_in_flight` opts in — and that opt-in is the
route to the contract's advice, because it keeps the one key across the retry.

Know what the opt-in costs, though: it is a property of the *policy*, not of one
bucket, so switching it on to get the blessed ``503`` retry also opts into
retrying every other completed 5xx and the client-side read timeout — class 4
entire, including the case this module calls the dangerous one. There is no
per-bucket switch, deliberately: which failures a deployment's key survives is a
fact about the deployment, not about the bucket, and a policy that claimed
otherwise would be guessing. Set it for a deployment documented to replay a
claimed key, not to reach a single bucket.

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

**The default budget is one server deadline window**, ten minutes — the same
number as :data:`~comfy_low.transport.MODEL_RUN_TIMEOUT`, deliberately, because
that is how long this surface is already willing to wait for one attempt. A
collect loop that outlives the deadline it is collecting after is the whole
point of class 3: the ``504`` says the server stopped holding *this* connection
at its own bound while the generation ran on, so a budget shorter than that bound
gives up mid-generation and hands the caller an error for work it will still be
charged for. The old 60-second budget was sized for the fast classes alone (a
connect failure, a paced ``429``) and could not outlast a single deadline
window. The cost of the larger default is paid in the *other* classes, and it is
worth naming: a genuinely unreachable server now spends up to ten minutes in
connect-and-back-off before it raises, where before it spent one. Pass
``RetryPolicy(max_elapsed=60.0)`` to get the old bound back.

Retry is on by default. ``Comfy(retry=NO_RETRY)`` turns it off; any other
policy is a :class:`RetryPolicy` you construct::

    from comfy_sdk import Comfy, NO_RETRY, RetryPolicy

    Comfy(retry=NO_RETRY)                              # exactly one attempt
    Comfy(retry=RetryPolicy(max_elapsed=60.0))         # give up after a minute
    Comfy(retry=RetryPolicy(retry_collectable=False))  # no 504/409 collect loop
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

#: The one 4xx another attempt can change on its own, and only when it carries
#: a pace.
_TOO_MANY_REQUESTS = 429

#: The 4xx the idempotency layer answers while the work the key already names is
#: still running. Retryable only when it carries a pace — see :func:`is_collectable`.
_CONFLICT = 409

#: Shared by the router's ``deadline_exceeded`` and ``provider_timeout`` buckets,
#: which is why the collect rule keys on the bucket and not on this.
_GATEWAY_TIMEOUT = 504

#: The one router bucket whose contract blesses a same-key resend: "retry it with
#: the SAME ``Idempotency-Key``: when the provider had already accepted the
#: generation, the retry collects that generation rather than dispatching
#: another" (``spec/router-openapi.yaml``).
_DEADLINE_EXCEEDED = "deadline_exceeded"

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

    This answers the *status* only, so it is still true of the one 5xx the
    router contract does characterise — a ``deadline_exceeded`` ``504`` naming a
    pace. :meth:`RetryPolicy.should_retry` consults :func:`is_collectable`
    first, which is where that response stops being "unknown"; the outcome of
    the *request* is genuinely still unknown there, and what the contract adds
    is that asking again with the same key is how you find it out.
    """
    return 500 <= status <= 599


def retry_after_of(exc: BaseException) -> float | None:
    """The pace the server named, in seconds, or ``None`` if it named none.

    Read off whatever exception carries it, by attribute rather than by type:
    the transport parses ``Retry-After`` into ``ApiError.retry_after`` for any
    status, and
    :func:`comfy_sdk.router_exceptions.error_from_response` puts the same header
    on ``RouterError.retry_after`` under the same name — so the throttled router
    buckets reach the ``429`` branch below rather than falling out of it for
    want of a pace. A value that is not a usable number of seconds is treated as
    absent rather than trusted into the arithmetic below.
    """
    raw = getattr(exc, "retry_after", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    seconds = float(raw)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def error_bucket_of(exc: BaseException) -> str | None:
    """The failure bucket the server named, or ``None`` if it named none.

    Read by attribute rather than by type, for the same reason
    :func:`retry_after_of` is: one failure reaches this module modelled by two
    different layers. A typed router error carries the wire ``error_type``
    (``RouterError.error_type``), while ``POST /models/run`` today raises the
    protocol :class:`~comfy_low.errors.ApiError`, whose envelope names the same
    thing ``code``. Reading only ``error_type`` would make every bucket-keyed
    rule below unreachable on the route those rules were written for — a retry
    that is a silent no-op with no test failing, which is the failure mode this
    module has already been bitten by twice.

    Both attribute names are namespaced enough for that to be safe: the buckets
    the rules below key on do not exist as v2 envelope codes, so a ``code`` that
    reads as a router bucket *is* one.
    """
    for attribute in ("error_type", "code"):
        raw = getattr(exc, attribute, None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def is_collectable(exc: BaseException) -> bool:
    """Whether the server said this failure's own work can be collected.

    True for the two answers that mean "the work your key already names is not
    finished; ask again for it", each of which the server pairs with the pace to
    ask at:

    * a router ``deadline_exceeded`` ``504`` carrying ``Retry-After`` — Comfy
      stopped holding the connection at its own bound while the generation ran
      on, and the contract says to "retry it with the SAME ``Idempotency-Key``",
      which "collects that generation rather than dispatching another";
    * a ``409`` carrying ``Retry-After`` — the idempotency layer's answer that
      the request under this key is still in progress, which is what the collect
      retry above meets when it arrives before the generation finishes.

    Both gates are load-bearing. The ``504`` must name its bucket because
    ``deadline_exceeded`` shares that status with ``provider_timeout``, where no
    contract blesses the resend and a header-less ``504`` from an intermediary is
    read as exactly that. The ``Retry-After`` must be present because the router
    sends it on a ``deadline_exceeded`` "only when Comfy holds a handle to a
    generation the provider is still running" — without it there is nothing to
    collect, and a resend would be dispatching new work rather than gathering
    old. A ``409`` with no pace is an ordinary conflict and stays a refusal.
    """
    status = getattr(exc, "http_status", None)
    if not isinstance(status, int) or retry_after_of(exc) is None:
        return False
    if status == _CONFLICT:
        return True
    return status == _GATEWAY_TIMEOUT and error_bucket_of(exc) == _DEADLINE_EXCEEDED


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
    #: disables retrying entirely (see :data:`NO_RETRY`). The default is one
    #: server deadline window — the same ten minutes as
    #: :data:`~comfy_low.transport.MODEL_RUN_TIMEOUT` — so that a collect loop
    #: after a ``deadline_exceeded`` ``504`` can outlast the bound that produced
    #: it; see this module's docstring for what that costs the other classes.
    max_elapsed: float = 600.0
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
    #: Also retry when the request's outcome is *unknown* — a completed 5xx that
    #: named no collectable pace, or a transport failure that may have delivered
    #: the request, so the server may still be generating. This is the class no
    #: contract characterises, which is what keeps it distinct from
    #: :attr:`retry_collectable`: there the server named the resend safe, here
    #: nothing did. Off by default, and the reason is the contract rather than
    #: caution: the v2 jobs contract makes ``Idempotency-Key`` single-use with no
    #: replay and keeps it *claimed* across exactly this class of failure — so a
    #: same-key retry here can come back ``422`` ``idempotency_key_reuse`` and
    #: hide the real error. Turn it on for a deployment that replays a repeated
    #: key instead of rejecting it — and raise ``max_elapsed`` when you do, since
    #: one full-length client timeout on a run spends the whole default budget on
    #: its own.
    retry_possibly_in_flight: bool = False
    #: Retry the failures the server itself paced for a same-key resend — a
    #: router ``deadline_exceeded`` ``504`` and an in-progress ``409``, each
    #: carrying ``Retry-After``. See :func:`is_collectable` for the exact gates.
    #: **On by default**, because this is the one class where the contract says
    #: the resend collects the generation already running rather than
    #: dispatching a second one, and the ``Retry-After`` is the server's own
    #: poll interval. Switch it off to have those answers raised to the caller
    #: instead — the whole retry surface goes away with ``NO_RETRY``. It is
    #: declared last, out of reading order, so that inserting it cannot change
    #: what an existing positional ``RetryPolicy(...)`` means.
    retry_collectable: bool = True

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
        an SDK exception, a router one) — so the status decides, with the
        failure bucket breaking the one tie a status cannot (the two ``504``
        buckets). Everything else is a transport failure, decided by whether the
        request can still be executing server-side. See this module's docstring
        for the five classes and why the key's contract, not the network, sorts
        them.
        """
        status = getattr(exc, "http_status", None)
        if isinstance(status, int):
            if status == _TOO_MANY_REQUESTS:
                # Rejected without starting work, so the contract released the
                # key and the same one is still spendable. Only with a pace
                # attached: the spec's retry signal is "429 + Retry-After",
                # and a 429 without one is not asking to be asked again.
                return retry_after_of(exc) is not None
            if self.retry_collectable and is_collectable(exc):
                # Checked before both branches below, because it overrides
                # both: a `deadline_exceeded` 504 is a 5xx the router contract
                # nevertheless blesses a same-key resend for, and an
                # in-progress 409 is a 4xx that does become true on a later
                # ask. Everything narrowing it to those two answers lives in
                # `is_collectable`.
                return True
            if is_unknown_outcome_status(status):
                # Every other 5xx, including the router's `service_unavailable`
                # 503: the bucket says the condition clears on its own, but
                # nothing says the Idempotency-Key does. See this module's
                # docstring.
                return self.retry_possibly_in_flight
            # Every other 4xx is deterministic — 404 (no such model), a
            # conflict that named no pace, 422 (invalid input), a
            # content-policy refusal — and none of them become true on the
            # second ask.
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
    "error_bucket_of",
    "is_collectable",
    "is_unknown_outcome_status",
    "retry_after_of",
]
