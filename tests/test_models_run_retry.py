"""Retrying a model run without paying for it twice.

The assertion this file exists for is
``test_every_attempt_of_one_call_sends_the_same_idempotency_key``: a retry that
mints a fresh key is indistinguishable, server-side, from a second order, and
on a surface where one call is a billed generation that is the difference
between a retry and a double charge. Everything else here is the policy that
decides *whether* to make that second attempt — 5xx and connect-phase failures
yes, a deterministic 4xx refusal no, a request that may still be running
server-side not unless asked — plus the elapsed-time bound that stops the
attempts stacking up.

The stub server in ``conftest.py`` drives the wire half (``model_run_fail_times``
for transient failure, ``model_run_error`` for a permanent one); the schedule
and deadline arithmetic is asserted directly against
:class:`~comfy_sdk.retry.RetryPolicy` and :class:`~comfy_sdk.retry.Retrier`,
where a fake clock makes it exact instead of timing-dependent.
"""

from __future__ import annotations

import time

import httpx
import pytest

from comfy_sdk import DEFAULT_RETRY, NO_RETRY, AsyncComfy, Comfy, RetryPolicy
from comfy_sdk.exceptions import ComfyError
from comfy_sdk.retry import Retrier, is_retryable_status
from comfy_sdk.router_exceptions import ContentPolicyViolation, InternalError


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


MODEL = "acme/flux/dev"
ARGS = {"prompt": "a cat", "steps": 4}

#: Retries the suite can afford to sit through: the schedule is asserted
#: against a fake clock elsewhere, so what these tests need from the real one
#: is only that the attempts happen at all.
FAST = RetryPolicy(max_elapsed=5.0, initial_backoff=0.01, max_backoff=0.02)


# --- the same key on every attempt of one call ---------------------------


def test_every_attempt_of_one_call_sends_the_same_idempotency_key(server) -> None:
    # The assertion that prevents the double charge: three attempts, one key.
    # A retry carrying a fresh key would read server-side as three separate
    # generations to run and bill.
    server.state.model_run_fail_times = 2
    with Comfy(retry=FAST) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    keys = server.state.model_run_idempotency_keys
    assert server.state.model_run_count == 3
    assert len(keys) == 3
    assert all(k for k in keys)
    assert len(set(keys)) == 1


async def test_every_attempt_of_one_async_call_sends_the_same_key(server) -> None:
    server.state.model_run_fail_times = 2
    async with AsyncComfy(retry=FAST) as client:
        assert await client.models.run(MODEL, ARGS) == server.state.model_run_result
    keys = server.state.model_run_idempotency_keys
    assert len(keys) == 3
    assert len(set(keys)) == 1


def test_a_second_call_is_a_new_key_even_though_both_retried(server) -> None:
    # The other half of the contract: a key is per logical call, not per
    # client and not per process. Two calls that each retried must not share
    # one — that would make the second call a replay of the first.
    server.state.model_run_fail_times = 1
    with Comfy(retry=FAST) as client:
        client.models.run(MODEL, ARGS)
        server.state.model_run_fail_times = 1
        client.models.run(MODEL, ARGS)
    keys = server.state.model_run_idempotency_keys
    first_call, second_call = keys[:2], keys[2:]
    assert len(set(first_call)) == 1
    assert len(set(second_call)) == 1
    assert set(first_call) != set(second_call)


async def test_a_second_async_call_is_a_new_key(server) -> None:
    async with AsyncComfy(retry=FAST) as client:
        await client.models.run(MODEL, ARGS)
        await client.models.run(MODEL, ARGS)
    first, second = server.state.model_run_idempotency_keys
    assert first != second


def test_an_explicit_key_is_reused_across_retries_verbatim(server) -> None:
    server.state.model_run_fail_times = 2
    with Comfy(retry=FAST) as client:
        client.models.run(MODEL, ARGS, idempotency_key="caller-chosen-07")
    assert server.state.model_run_idempotency_keys == ["caller-chosen-07"] * 3


# --- what gets retried ---------------------------------------------------


def test_retry_is_on_by_default(server) -> None:
    # No `retry=` argument anywhere: a transient failure is survived out of
    # the box, which is the point of the default being on.
    server.state.model_run_fail_times = 1
    with Comfy() as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2


def test_the_default_policy_retries_and_bounds_by_elapsed_time() -> None:
    assert DEFAULT_RETRY.enabled
    assert DEFAULT_RETRY.max_elapsed > 0
    assert DEFAULT_RETRY.jitter
    # Off by default: retrying a request that may still be generating is only
    # safe against a server that replays the key rather than re-running it.
    assert not DEFAULT_RETRY.retry_possibly_in_flight


def test_retry_can_be_disabled(server) -> None:
    server.state.model_run_fail_times = 1
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


async def test_retry_can_be_disabled_on_the_async_client(server) -> None:
    server.state.model_run_fail_times = 1
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError):
            await client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


def test_the_policy_is_readable_off_the_namespace(server) -> None:
    with Comfy(retry=NO_RETRY) as client:
        assert client.models.retry is NO_RETRY
    with Comfy() as client:
        assert client.models.retry is DEFAULT_RETRY


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "content_policy_violation"),
        (404, "not_found"),
        (409, "hash_mismatch"),
        (422, "invalid_workflow"),
        (401, "unauthorized"),
        (402, "insufficient_credits"),
    ],
    ids=lambda v: str(v),
)
def test_a_deterministic_refusal_is_never_retried(server, status: int, code: str) -> None:
    # Asking again cannot change any of these answers, and on a billed surface
    # a retry of a refusal spends money to be refused again.
    server.state.model_run_error = (status, code)
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


def test_a_429_is_not_retried_by_this_policy(server) -> None:
    # Deliberate: a 429 names its own pace in `Retry-After`, and honouring that
    # is a different mechanism from the blind backoff here. Blindly retrying it
    # would ignore the pace the server asked for.
    server.state.model_run_error = (429, "queue_full")
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


async def test_a_deterministic_refusal_is_never_retried_on_the_async_client(server) -> None:
    server.state.model_run_error = (422, "invalid_workflow")
    async with AsyncComfy(retry=FAST) as client:
        with pytest.raises(ComfyError):
            await client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_retried(server, status: int) -> None:
    server.state.model_run_transient_error = (status, "internal_error")
    server.state.model_run_fail_times = 1
    with Comfy(retry=FAST) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2


def test_a_permanent_5xx_eventually_surfaces_as_the_sdk_error(server) -> None:
    server.state.model_run_error = (500, "internal_error")
    policy = RetryPolicy(max_elapsed=0.2, initial_backoff=0.01, max_backoff=0.02)
    with Comfy(retry=policy) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.http_status == 500
    # It gave up rather than looping forever, and it did try more than once.
    assert server.state.model_run_count > 1
    assert len(set(server.state.model_run_idempotency_keys)) == 1


# --- the long client timeout: a retry must not race the original ---------


def test_a_client_timeout_is_not_retried_by_default(server) -> None:
    # A run holds the connection while the server generates, so a client-side
    # timeout means "no answer yet", not "it did not happen". The server may
    # still be generating, so the default policy does not start a second one.
    server.state.model_run_delay = 1.0
    with Comfy(retry=FAST) as client:
        with pytest.raises(httpx.TimeoutException):
            client.models.run(MODEL, ARGS, timeout=0.15)
    assert server.state.model_run_count == 1


async def test_an_async_client_timeout_is_not_retried_by_default(server) -> None:
    server.state.model_run_delay = 1.0
    async with AsyncComfy(retry=FAST) as client:
        with pytest.raises(httpx.TimeoutException):
            await client.models.run(MODEL, ARGS, timeout=0.15)
    assert server.state.model_run_count == 1


def test_opting_in_retries_a_client_timeout_under_the_one_key(server) -> None:
    # The opt-in for a server that replays a repeated key. Even here the key is
    # unchanged, so the second attempt is the same request, not a second order.
    server.state.model_run_delay = 1.5
    policy = RetryPolicy(
        max_elapsed=1.0,
        initial_backoff=0.01,
        max_backoff=0.02,
        retry_possibly_in_flight=True,
    )
    with Comfy(retry=policy) as client:
        with pytest.raises(httpx.TimeoutException):
            client.models.run(MODEL, ARGS, timeout=0.15)
    assert server.state.model_run_count >= 2
    assert len(set(server.state.model_run_idempotency_keys)) == 1


def test_the_retry_deadline_never_interrupts_an_attempt(server) -> None:
    # The deadline is checked between attempts only. An attempt that outlives
    # the whole retry budget still runs to its own timeout and returns — the
    # SDK never abandons a request that is merely slow, which is what would
    # leave a generation running with nobody waiting for it.
    server.state.model_run_delay = 0.4
    policy = RetryPolicy(max_elapsed=0.05, initial_backoff=0.01, max_backoff=0.02)
    with Comfy(retry=policy) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 1


# --- bounded by elapsed time, not by attempt count -----------------------


def test_retries_stop_at_the_elapsed_budget_not_at_an_attempt_count(server) -> None:
    # A per-attempt budget multiplies: N attempts each granted their own wait
    # stack into a total nobody chose. This bound is wall-clock across the
    # whole call, so however cheap an attempt is, the call still ends.
    server.state.model_run_error = (503, "internal_error")
    policy = RetryPolicy(max_elapsed=0.3, initial_backoff=0.01, max_backoff=0.02)
    started = time.monotonic()
    with Comfy(retry=policy) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    elapsed = time.monotonic() - started
    # Generous upper bound: what matters is that it is bounded by the budget
    # rather than by however many cheap attempts fit in it.
    assert elapsed < 5.0
    assert server.state.model_run_count > 1


def test_the_deadline_is_measured_from_the_first_attempt() -> None:
    clock = _FakeClock()
    policy = RetryPolicy(max_elapsed=10.0, initial_backoff=1.0, max_backoff=1.0, jitter=False)
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    failure = InternalError("boom", http_status=500)

    # Nine one-second waits fit inside the ten-second budget...
    for _ in range(9):
        delay = retrier.delay_before_retry(failure)
        assert delay == 1.0
        clock.advance(delay)
    # ...and the tenth would start an attempt at the deadline, so it does not.
    assert retrier.delay_before_retry(failure) is None
    assert retrier.attempts == 10


def test_a_slow_attempt_spends_the_budget_it_actually_used() -> None:
    # The bound is elapsed time, so one attempt that takes most of the budget
    # leaves no room for another — however few attempts have been made.
    clock = _FakeClock()
    policy = RetryPolicy(max_elapsed=10.0, initial_backoff=1.0, max_backoff=1.0, jitter=False)
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    clock.advance(9.5)
    assert retrier.delay_before_retry(InternalError("boom", http_status=500)) is None


def test_a_disabled_policy_never_yields_a_delay() -> None:
    retrier = Retrier(NO_RETRY, now=_FakeClock())
    assert retrier.delay_before_retry(InternalError("boom", http_status=500)) is None


# --- backoff and jitter --------------------------------------------------


def test_backoff_grows_and_holds_at_the_ceiling() -> None:
    policy = RetryPolicy(initial_backoff=1.0, backoff_factor=2.0, max_backoff=8.0, jitter=False)
    schedule = [policy.backoff(n) for n in range(1, 7)]
    assert schedule == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_backoff_cannot_overflow_on_a_pathological_attempt_count() -> None:
    policy = RetryPolicy(initial_backoff=1.0, backoff_factor=2.0, max_backoff=8.0, jitter=False)
    assert policy.backoff(100_000) == 8.0


def test_jitter_spreads_each_delay_below_its_ceiling() -> None:
    # Full jitter: identical clients that failed together must not come back
    # together and re-flatten a server that is recovering.
    policy = RetryPolicy(initial_backoff=1.0, backoff_factor=2.0, max_backoff=8.0)
    delays = [policy.backoff(3) for _ in range(200)]
    ceiling = 4.0
    assert all(0.0 <= d <= ceiling for d in delays)
    assert len(set(delays)) > 1
    assert min(delays) < ceiling / 2 < max(delays)


def test_jitter_can_be_turned_off_for_a_deterministic_schedule() -> None:
    policy = RetryPolicy(initial_backoff=1.0, backoff_factor=2.0, max_backoff=8.0, jitter=False)
    assert len({policy.backoff(2) for _ in range(50)}) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_elapsed": -1.0},
        {"initial_backoff": 0.0},
        {"initial_backoff": -1.0},
        {"backoff_factor": 0.5},
        {"max_backoff": 0.1},
    ],
    ids=["negative-budget", "zero-backoff", "negative-backoff", "shrinking", "ceiling-below-floor"],
)
def test_an_incoherent_policy_is_rejected_at_construction(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_a_policy_is_immutable() -> None:
    # Shared by every call on a client, so one call cannot rewrite another's.
    with pytest.raises(AttributeError):
        DEFAULT_RETRY.max_elapsed = 1.0


# --- classification ------------------------------------------------------


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 599])
def test_5xx_is_retryable(status: int) -> None:
    assert is_retryable_status(status)


@pytest.mark.parametrize("status", [200, 400, 401, 402, 403, 404, 409, 422, 429, 499])
def test_everything_below_500_is_not(status: int) -> None:
    assert not is_retryable_status(status)


def test_a_typed_router_refusal_is_classified_by_its_status() -> None:
    # The classifier reads `http_status` off whatever exception carries it, so
    # a typed router error is judged the same way as the protocol error the
    # model surface raises today.
    assert not DEFAULT_RETRY.should_retry(ContentPolicyViolation("refused", http_status=400))
    assert DEFAULT_RETRY.should_retry(InternalError("boom", http_status=500))


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("too slow to connect"),
        httpx.PoolTimeout("no connection free"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_connect_phase_failure_is_retryable(exc: Exception) -> None:
    # The request was never delivered, so there is nothing running server-side
    # for a retry to duplicate.
    assert DEFAULT_RETRY.should_retry(exc)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("no answer"),
        httpx.ReadError("connection dropped"),
        httpx.WriteError("connection dropped"),
        httpx.RemoteProtocolError("truncated"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_possibly_in_flight_failure_needs_the_opt_in(exc: Exception) -> None:
    assert not DEFAULT_RETRY.should_retry(exc)
    opted_in = RetryPolicy(retry_possibly_in_flight=True)
    assert opted_in.should_retry(exc)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.LocalProtocolError("the client built a bad request"),
        httpx.UnsupportedProtocol("no such scheme"),
        ValueError("not a transport failure at all"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_a_local_failure_is_never_retryable(exc: Exception) -> None:
    # A bug on this side of the wire does not become correct on the second try,
    # and retrying it hides it.
    opted_in = RetryPolicy(retry_possibly_in_flight=True)
    assert not DEFAULT_RETRY.should_retry(exc)
    assert not opted_in.should_retry(exc)
