"""Retrying a model run without paying for it twice.

The assertion this file exists for is
``test_every_attempt_of_one_call_sends_the_same_idempotency_key``: a retry that
mints a fresh key is indistinguishable, server-side, from a second order, and
on a surface where one call is a billed generation that is the difference
between a retry and a double charge.

Everything else here is the policy that decides *whether* to make that second
attempt, and the thing that decides it is the key's own contract rather than a
guess about the network. ``spec/openapi.yaml`` makes the v2 jobs API's
``Idempotency-Key`` single-use, reject-on-duplicate, with no response replay,
and says which failures release the key (a definitive reject that started no
work) and which keep it claimed (a 5xx or upstream timeout, where the outcome is
unknown). ``spec/router-openapi.yaml`` states the one exception: a
``deadline_exceeded`` ``504`` carrying ``Retry-After`` is to be retried "with
the SAME ``Idempotency-Key``", which collects the generation already running.
So the default policy retries what the one key survives — a connect-phase
failure, a ``429`` that names its own ``Retry-After``, and the collectable
answers that name one (that ``504``, and the in-progress ``409`` a collect
attempt meets) — and everything in the unknown-outcome class sits behind
``retry_possibly_in_flight``, for the deployment that replays a repeated key
instead of rejecting it.

The stub server in ``conftest.py`` drives the wire half and now enforces that
same reject-on-duplicate rule (``model_run_replays_idempotency_key`` switches
it to the replaying deployment, ``model_run_collects_after_deadline`` to the
narrower carry the router describes for its own deadline), so a retry design the
real server would reject cannot pass here. The never-delivered class is asserted
against ``_FlakyLow`` instead: a stub HTTP server that is up cannot refuse a
connection. The schedule and deadline arithmetic is asserted directly against
:class:`~comfy_sdk.retry.RetryPolicy` and :class:`~comfy_sdk.retry.Retrier`,
where a fake clock makes it exact instead of timing-dependent.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

import httpx
import pytest

from comfy_low.errors import ApiError
from comfy_low.transport import MODEL_RUN_TIMEOUT
from comfy_sdk import DEFAULT_RETRY, NO_RETRY, AsyncComfy, Comfy, RetryPolicy
from comfy_sdk.exceptions import ComfyError, IdempotencyKeyReuse
from comfy_sdk.models import AsyncModels, Models
from comfy_sdk.retry import (
    Retrier,
    error_bucket_of,
    is_collectable,
    is_unknown_outcome_status,
    retry_after_of,
)
from comfy_sdk.router_exceptions import (
    ContentPolicyViolation,
    DeadlineExceeded,
    InternalError,
    ProviderTimeout,
    RouterError,
    ServiceUnavailable,
)


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _FlakyLow:
    """A transport whose first ``fail_times`` attempts never reach the server.

    The default policy's retry surface is the never-delivered class — the one
    place a repeated ``Idempotency-Key`` is provably still spendable, because
    the request never got far enough for the server to claim it. A stub HTTP
    server that is listening cannot produce that failure, so the one-key
    contract is asserted against a transport that can.
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.keys: list[str | None] = []
        self.payloads: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {"images": [{"url": "http://example.invalid/x.png"}]}

    def _attempt(self, arguments: Mapping[str, Any], key: str | None) -> dict[str, Any]:
        self.keys.append(key)
        self.payloads.append(dict(arguments))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise httpx.ConnectError("connection refused")
        return self.result

    def post_model_run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: Any = None,
    ) -> dict[str, Any]:
        return self._attempt(arguments, idempotency_key)


class _AsyncFlakyLow(_FlakyLow):
    async def post_model_run(  # type: ignore[override]
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: Any = None,
    ) -> dict[str, Any]:
        return self._attempt(arguments, idempotency_key)


#: The canonical two-segment `{provider}/{model}` id the route is addressed by.
MODEL = "acme/flux-dev"
ARGS = {"prompt": "a cat", "steps": 4}

#: Retries the suite can afford to sit through: the schedule is asserted
#: against a fake clock elsewhere, so what these tests need from the real one
#: is only that the attempts happen at all.
FAST = RetryPolicy(max_elapsed=5.0, initial_backoff=0.01, max_backoff=0.02)

#: ``FAST`` for a deployment that replays a repeated key, which is the only
#: kind against which retrying an unknown outcome is correct.
FAST_OPTED_IN = RetryPolicy(
    max_elapsed=5.0, initial_backoff=0.01, max_backoff=0.02, retry_possibly_in_flight=True
)


def _models(low: _FlakyLow, policy: RetryPolicy = FAST) -> Models:
    return Models(cast(Any, low), policy)


def _async_models(low: _AsyncFlakyLow, policy: RetryPolicy = FAST) -> AsyncModels:
    return AsyncModels(cast(Any, low), policy)


# --- the same key on every attempt of one call ---------------------------


def test_every_attempt_of_one_call_sends_the_same_idempotency_key() -> None:
    # The assertion that prevents the double charge: three attempts, one key.
    # A retry carrying a fresh key would read server-side as three separate
    # generations to run and bill.
    low = _FlakyLow(fail_times=2)
    assert _models(low).run(MODEL, ARGS) == low.result
    assert len(low.keys) == 3
    assert all(k for k in low.keys)
    assert len(set(low.keys)) == 1


async def test_every_attempt_of_one_async_call_sends_the_same_key() -> None:
    low = _AsyncFlakyLow(fail_times=2)
    assert await _async_models(low).run(MODEL, ARGS) == low.result
    assert len(low.keys) == 3
    assert len(set(low.keys)) == 1


def test_a_second_call_is_a_new_key_even_though_both_retried() -> None:
    # The other half of the contract: a key is per logical call, not per
    # client and not per process. Two calls that each retried must not share
    # one — that would make the second call a replay of the first.
    low = _FlakyLow(fail_times=1)
    models = _models(low)
    models.run(MODEL, ARGS)
    low.fail_times = 1
    models.run(MODEL, ARGS)
    first_call, second_call = low.keys[:2], low.keys[2:]
    assert len(set(first_call)) == 1
    assert len(set(second_call)) == 1
    assert set(first_call) != set(second_call)


async def test_a_second_async_call_is_a_new_key(server) -> None:
    async with AsyncComfy() as client:
        await client.models.run(MODEL, ARGS)
        await client.models.run(MODEL, ARGS)
    first, second = server.state.model_run_idempotency_keys
    assert first != second


def test_an_explicit_key_is_reused_across_retries_verbatim() -> None:
    low = _FlakyLow(fail_times=2)
    _models(low).run(MODEL, ARGS, idempotency_key="caller-chosen-07")
    assert low.keys == ["caller-chosen-07"] * 3


def test_the_body_is_frozen_before_the_first_attempt() -> None:
    # A mutation between attempts would put a different body under the *same*
    # key, which is the same-key-different-body case the contract rejects
    # outright — so the payload is snapshotted where the key is minted.
    arguments: dict[str, Any] = {"prompt": "a cat"}

    class _MutatingLow(_FlakyLow):
        def _attempt(self, args: Mapping[str, Any], key: str | None) -> dict[str, Any]:
            result = super()._attempt(args, key)
            arguments["prompt"] = "something else entirely"
            return result

    low = _MutatingLow(fail_times=2)
    _models(low).run(MODEL, arguments)
    assert [p["prompt"] for p in low.payloads] == ["a cat"] * 3


# --- what gets retried ---------------------------------------------------


def test_retry_is_on_by_default(server) -> None:
    # No `retry=` argument anywhere: a transient failure is survived out of
    # the box, which is the point of the default being on. A 429 that names a
    # pace is the wire-level failure the *default* policy retries — the
    # contract releases the key for a reject that started no work.
    server.state.model_run_fail_times = 1
    server.state.model_run_transient_error = (429, "concurrency_limit_exceeded")
    server.state.model_run_retry_after = "0"
    with Comfy() as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2
    assert len(set(server.state.model_run_idempotency_keys)) == 1


def test_the_default_policy_retries_and_bounds_by_elapsed_time() -> None:
    assert DEFAULT_RETRY.enabled
    assert DEFAULT_RETRY.max_elapsed > 0
    assert DEFAULT_RETRY.jitter
    # Off by default: retrying a request whose outcome is unknown is only
    # correct against a server that replays the key rather than rejecting it.
    assert not DEFAULT_RETRY.retry_possibly_in_flight


def test_retry_can_be_disabled(server) -> None:
    server.state.model_run_fail_times = 1
    server.state.model_run_transient_error = (429, "concurrency_limit_exceeded")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


async def test_retry_can_be_disabled_on_the_async_client(server) -> None:
    server.state.model_run_fail_times = 1
    server.state.model_run_transient_error = (429, "concurrency_limit_exceeded")
    server.state.model_run_retry_after = "0"
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


def test_a_429_without_a_pace_is_not_retried(server) -> None:
    # The spec's retry signal is "429 + Retry-After". One without a pace is
    # not asking to be asked again, and guessing a delay for it is the
    # thundering-herd case full jitter exists to avoid.
    server.state.model_run_error = (429, "queue_full")
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


def test_a_429_that_names_a_pace_is_retried_at_that_pace(server) -> None:
    # The transport already parses Retry-After onto the error; discarding it
    # and backing off blind is what would re-flatten a server that just told
    # us how fast to come back.
    server.state.model_run_fail_times = 2
    server.state.model_run_transient_error = (429, "queue_full")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=FAST) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 3
    assert len(set(server.state.model_run_idempotency_keys)) == 1


def test_the_server_named_pace_is_used_instead_of_the_backoff_schedule() -> None:
    clock = _FakeClock()
    policy = RetryPolicy(max_elapsed=60.0, initial_backoff=1.0, max_backoff=2.0, jitter=False)
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    paced = ComfyError("slow down", code="queue_full", http_status=429)
    paced.retry_after = 7  # type: ignore[attr-defined]
    # 7s is well above `max_backoff`: a pace the server named is honoured as
    # given rather than capped by a ceiling meant for our own guesswork.
    assert retrier.delay_before_retry(paced) == 7.0


async def test_a_deterministic_refusal_is_never_retried_on_the_async_client(server) -> None:
    server.state.model_run_error = (422, "invalid_workflow")
    async with AsyncComfy(retry=FAST) as client:
        with pytest.raises(ComfyError):
            await client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_not_retried_against_a_reject_on_duplicate_server(server, status: int) -> None:
    # The heart of it. The contract keeps the key *claimed* when the outcome is
    # unknown, so a same-key retry after a 5xx cannot succeed — it comes back
    # 422 idempotency_key_reuse and replaces the real error with a confusing
    # one. So the default policy does not make it, and the caller sees the 5xx.
    server.state.model_run_error = (status, "internal_error")
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1
    assert excinfo.value.http_status == status
    assert not isinstance(excinfo.value, IdempotencyKeyReuse)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_retried_under_the_replay_opt_in(server, status: int) -> None:
    server.state.model_run_replays_idempotency_key = True
    server.state.model_run_transient_error = (status, "internal_error")
    server.state.model_run_fail_times = 1
    with Comfy(retry=FAST_OPTED_IN) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2
    assert len(set(server.state.model_run_idempotency_keys)) == 1


def test_a_permanent_5xx_eventually_surfaces_as_the_sdk_error(server) -> None:
    server.state.model_run_replays_idempotency_key = True
    server.state.model_run_error = (500, "internal_error")
    policy = RetryPolicy(
        max_elapsed=0.2, initial_backoff=0.01, max_backoff=0.02, retry_possibly_in_flight=True
    )
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
    server.state.model_run_replays_idempotency_key = True
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
    server.state.model_run_replays_idempotency_key = True
    server.state.model_run_error = (503, "internal_error")
    policy = RetryPolicy(
        max_elapsed=0.3, initial_backoff=0.01, max_backoff=0.02, retry_possibly_in_flight=True
    )
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
    policy = RetryPolicy(
        max_elapsed=10.0,
        initial_backoff=1.0,
        max_backoff=1.0,
        jitter=False,
        retry_possibly_in_flight=True,
    )
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    failure = InternalError("boom", http_status=500)

    # Ten one-second waits fit inside the ten-second budget...
    for _ in range(10):
        delay = retrier.delay_before_retry(failure)
        assert delay == 1.0
        clock.advance(delay)
    # ...and by then the budget is spent, so no further attempt starts.
    assert retrier.delay_before_retry(failure) is None
    assert retrier.attempts == 11


def test_a_delay_that_would_overshoot_is_clamped_to_what_is_left() -> None:
    # Clamped rather than abandoned: with full jitter drawn over [0, ceiling],
    # one unlucky draw would otherwise end a call that still had seconds of
    # budget, making identical calls take a randomly varying number of
    # attempts. `client.py::_retry_delay` bounds its 429 wait the same way.
    clock = _FakeClock()
    policy = RetryPolicy(
        max_elapsed=10.0,
        initial_backoff=1.0,
        max_backoff=1.0,
        jitter=False,
        retry_possibly_in_flight=True,
    )
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    clock.advance(9.5)
    assert retrier.delay_before_retry(InternalError("boom", http_status=500)) == 0.5


def test_a_spent_budget_yields_no_delay_at_all() -> None:
    clock = _FakeClock()
    policy = RetryPolicy(max_elapsed=10.0, retry_possibly_in_flight=True)
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    clock.advance(10.0)
    assert retrier.delay_before_retry(InternalError("boom", http_status=500)) is None


def test_a_short_budget_still_retries_rather_than_reporting_a_lie() -> None:
    # `enabled` is True here, and now it means something: the first delay is
    # clamped into the budget instead of the policy silently never retrying.
    policy = RetryPolicy(max_elapsed=0.3, retry_possibly_in_flight=True)
    assert policy.enabled
    retrier = Retrier(policy, now=_FakeClock(), rng=lambda: 1.0)
    delay = retrier.delay_before_retry(InternalError("boom", http_status=500))
    assert delay is not None
    assert 0 < delay <= 0.3


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


def test_backoff_cannot_overflow_on_a_pathological_factor() -> None:
    # `initial_backoff * backoff_factor**exponent` is fully evaluated before
    # any `min()` could cap it, and CPython's float `**` raises OverflowError
    # rather than saturating — which would escape the retry loop and replace
    # the genuine API error with an arithmetic one.
    policy = RetryPolicy(initial_backoff=1.0, backoff_factor=1e200, max_backoff=8.0, jitter=False)
    assert [policy.backoff(n) for n in range(1, 6)] == [1.0, 8.0, 8.0, 8.0, 8.0]


def test_a_gentle_factor_keeps_growing_past_a_large_attempt_count() -> None:
    # Clamping the exponent instead would freeze growth long before the
    # ceiling for any factor that climbs slowly.
    policy = RetryPolicy(initial_backoff=0.5, backoff_factor=1.01, max_backoff=15.0, jitter=False)
    assert policy.backoff(66) > policy.backoff(65) > policy.backoff(64)
    assert policy.backoff(1000) == 15.0


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
        {"collect_max_elapsed": -1.0},
        {"initial_backoff": 0.0},
        {"initial_backoff": -1.0},
        {"backoff_factor": 0.5},
        {"max_backoff": 0.1},
    ],
    ids=[
        "negative-budget",
        "negative-collect-budget",
        "zero-backoff",
        "negative-backoff",
        "shrinking",
        "ceiling-below-floor",
    ],
)
def test_an_incoherent_policy_is_rejected_at_construction(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        # Infinity passes every ordered check and never reaches its deadline,
        # so `run` would retry a permanently failing server forever.
        {"max_elapsed": float("inf")},
        # NaN fails every ordered check, so it constructs and then misbehaves:
        # a NaN budget reads as "retrying disabled", a NaN backoff makes the
        # caller's `sleep` raise in place of the real error.
        {"max_elapsed": float("nan")},
        {"collect_max_elapsed": float("inf")},
        {"collect_max_elapsed": float("nan")},
        {"initial_backoff": float("nan")},
        {"backoff_factor": float("nan")},
        {"max_backoff": float("nan")},
        {"max_backoff": float("inf")},
        {"backoff_factor": float("inf")},
    ],
    ids=lambda v: str(v),
)
def test_a_non_finite_policy_is_rejected_at_construction(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_a_policy_is_immutable() -> None:
    # Shared by every call on a client, so one call cannot rewrite another's.
    with pytest.raises(AttributeError):
        DEFAULT_RETRY.max_elapsed = 1.0


# --- classification ------------------------------------------------------


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 599])
def test_5xx_leaves_the_outcome_unknown(status: int) -> None:
    assert is_unknown_outcome_status(status)
    # ...and so is not retried unless the deployment replays repeated keys.
    assert not DEFAULT_RETRY.should_retry(InternalError("boom", http_status=status))
    opted_in = RetryPolicy(retry_possibly_in_flight=True)
    assert opted_in.should_retry(InternalError("boom", http_status=status))


@pytest.mark.parametrize("status", [200, 400, 401, 402, 403, 404, 409, 422, 429, 499])
def test_everything_below_500_has_a_known_outcome(status: int) -> None:
    assert not is_unknown_outcome_status(status)


@pytest.mark.parametrize(
    "raw,expected",
    [(0, 0.0), (5, 5.0), (2.5, 2.5), (None, None), (-1, None), (float("nan"), None), ("3", None)],
    ids=lambda v: str(v),
)
def test_a_named_pace_is_read_off_the_error_or_treated_as_absent(raw, expected) -> None:
    exc = ComfyError("throttled", code="queue_full", http_status=429)
    exc.retry_after = raw  # type: ignore[attr-defined]
    assert retry_after_of(exc) == expected


def test_a_typed_router_refusal_is_classified_by_its_status() -> None:
    # The classifier reads `http_status` off whatever exception carries it, so
    # a typed router error is judged the same way as the protocol error the
    # model surface raises today.
    assert not DEFAULT_RETRY.should_retry(ContentPolicyViolation("refused", http_status=400))
    opted_in = RetryPolicy(retry_possibly_in_flight=True)
    assert opted_in.should_retry(InternalError("boom", http_status=500))


# --- the router bucket that asks to be retried ---------------------------


def test_service_unavailable_is_not_retried_by_default() -> None:
    # The DECISION this asserts, so it cannot be changed silently: the router
    # contract tells a caller to retry `service_unavailable` with backoff, and
    # the SDK does not make that retry for them. It arrives on a 503, and the
    # question the default policy answers first is whether the one
    # Idempotency-Key is still spendable -- which no contract says a 503 leaves
    # it. See comfy_sdk.retry's module docstring.
    exc = ServiceUnavailable("a dependency is briefly unavailable", http_status=503)
    assert not DEFAULT_RETRY.should_retry(exc)
    assert not FAST.should_retry(exc)


def test_service_unavailable_is_retried_under_the_replay_opt_in_on_one_key() -> None:
    # The opt-in is the caller's route to the contract's advice, and it stays
    # subject to the rule the whole module exists for: the second attempt
    # carries the SAME key, so a retry cannot be billed as a second generation.
    class _UnavailableThenFine(_FlakyLow):
        def _attempt(self, args: Mapping[str, Any], key: str | None) -> dict[str, Any]:
            self.keys.append(key)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise ServiceUnavailable("try again shortly", http_status=503)
            return self.result

    low = _UnavailableThenFine(fail_times=1)
    assert _models(low, FAST_OPTED_IN).run(MODEL, ARGS) == low.result
    assert len(low.keys) == 2
    # Spelled out rather than `len(set(low.keys)) == 1`, which is also true of
    # `[None, None]` -- i.e. of a run that sent no `Idempotency-Key` at all.
    # The property under test is that a key was sent AND that the retry reused
    # it; a set-size check alone would still pass if the header vanished.
    assert low.keys[0] is not None
    assert low.keys[1] == low.keys[0]


def test_a_router_error_reaches_the_retry_loop_at_all() -> None:
    # Router errors derive from ComfyError, not ApiError, so a `run` that only
    # caught ApiError would never hand one to the policy — making the branch
    # above unreachable and retry a silent no-op the day this route raises
    # them.
    class _RouterFailingLow(_FlakyLow):
        def _attempt(self, args: Mapping[str, Any], key: str | None) -> dict[str, Any]:
            self.keys.append(key)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise InternalError("upstream blew up", http_status=500)
            return self.result

    low = _RouterFailingLow(fail_times=1)
    policy = RetryPolicy(
        max_elapsed=5.0, initial_backoff=0.01, max_backoff=0.02, retry_possibly_in_flight=True
    )
    assert _models(low, policy).run(MODEL, ARGS) == low.result
    assert len(low.keys) == 2
    assert len(set(low.keys)) == 1


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
    # for a retry to duplicate and the key was never claimed.
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


# --- the collect loop: the failures the server paced for a same-key resend ---
#
# The router contract blesses exactly one same-key resend: `deadline_exceeded`,
# where "the retry collects that generation rather than dispatching another"
# and the `Retry-After` on the 504 "says when to ask". These assert that the
# default policy makes that retry, that both gates on it hold (the bucket and
# the pace), and that the 409 the idempotency layer answers mid-collect is
# waited out the same way -- so one `run()` rides the loop to the result.


def test_a_deadline_504_that_names_a_pace_is_collected_under_the_one_key(server) -> None:
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_transient_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    server.state.model_run_fail_times = 1
    with Comfy(retry=FAST) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2
    # Spelled out rather than `len(set(...)) == 1`, which is also true of
    # `[None, None]`: the property is that a key was sent AND that the collect
    # attempt presented the SAME one, because that is what makes it a
    # collection of the first generation rather than a second order.
    first, second = server.state.model_run_idempotency_keys
    assert first is not None
    assert second == first


async def test_a_deadline_504_is_collected_on_the_async_client(server) -> None:
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_transient_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    server.state.model_run_fail_times = 1
    async with AsyncComfy(retry=FAST) as client:
        assert await client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2
    first, second = server.state.model_run_idempotency_keys
    assert first is not None
    assert second == first


def test_a_deadline_504_without_a_pace_is_not_retried(server) -> None:
    # The router sends `Retry-After` on this bucket only when it holds a handle
    # to a generation still running. Without one there is nothing to collect,
    # so the resend would be dispatching new work -- back to the unknown-outcome
    # class, where the caller sees the 504.
    server.state.model_run_error = (504, "deadline_exceeded")
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1
    assert excinfo.value.http_status == 504


def test_a_504_that_is_not_the_deadline_bucket_is_not_collected(server) -> None:
    # The gate that cannot be dropped: `deadline_exceeded` shares 504 with
    # `provider_timeout`, and a header-less 504 from an intermediary reads as
    # the latter. Only the bucket the contract blesses is resent, however
    # helpfully the response is paced.
    server.state.model_run_error = (504, "provider_timeout")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1
    assert excinfo.value.http_status == 504


def test_an_in_progress_409_that_names_a_pace_is_waited_out_and_retried(server) -> None:
    # The answer the collect retry meets when it arrives before the generation
    # has finished: the key is recognised, the work is still running, come back
    # in `Retry-After`. One `run()` rides that to the 200.
    server.state.model_run_transient_error = (409, "generation_in_progress")
    server.state.model_run_retry_after = "0"
    server.state.model_run_fail_times = 2
    with Comfy(retry=FAST) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 3
    keys = server.state.model_run_idempotency_keys
    assert keys[0] is not None
    assert keys == [keys[0]] * 3


def test_a_409_that_names_no_pace_is_still_a_refusal(server) -> None:
    # Unchanged: an ordinary conflict is deterministic. The pace is the whole
    # signal that this one is "not yet" rather than "no".
    server.state.model_run_error = (409, "generation_in_progress")
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


def test_no_retry_disables_the_collect_loop(server) -> None:
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


def test_the_collect_loop_can_be_switched_off_in_the_policy(server) -> None:
    # No feature flag: the new default is a `RetryPolicy` knob like every other
    # one, so a caller who wants the 504 raised keeps the rest of the policy.
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    policy = RetryPolicy(
        max_elapsed=5.0, initial_backoff=0.01, max_backoff=0.02, retry_collectable=False
    )
    with Comfy(retry=policy) as client:
        with pytest.raises(ComfyError):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1


def test_the_collect_loop_gives_up_at_the_elapsed_budget(server) -> None:
    # A generation that outlives the budget still ends the call: `max_elapsed`
    # bounds the loop however patiently the server keeps saying "not yet".
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "1"
    # `collect_max_elapsed`, not `max_elapsed`: the collect class has its own
    # budget, and this is the bound that ends this loop.
    policy = RetryPolicy(
        max_elapsed=0.4, collect_max_elapsed=0.4, initial_backoff=0.01, max_backoff=0.02
    )
    started = time.monotonic()
    with Comfy(retry=policy) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert time.monotonic() - started < 5.0
    assert excinfo.value.http_status == 504
    assert server.state.model_run_count >= 2
    assert len(set(server.state.model_run_idempotency_keys)) == 1


def test_the_budget_bounds_the_loop_even_at_the_server_named_pace() -> None:
    # The same bound, exactly, against a fake clock: three full 30s waits fit
    # in a 100s budget and the fourth is clamped to the 10s left rather than
    # overshooting it, after which no further attempt starts.
    clock = _FakeClock()
    retrier = Retrier(RetryPolicy(collect_max_elapsed=100.0), now=clock, rng=lambda: 1.0)
    failure = DeadlineExceeded("still generating", http_status=504, retry_after=30)
    delays = []
    while (delay := retrier.delay_before_retry(failure)) is not None:
        delays.append(delay)
        clock.advance(delay)
    assert delays == [30.0, 30.0, 30.0, 10.0]


def test_the_collect_budget_outlasts_a_server_deadline_window() -> None:
    # The decision this asserts, so it cannot drift silently. A `deadline_exceeded`
    # 504 arrives AT the server's own bound -- the same ten minutes `run` is
    # already willing to spend on one attempt -- so a collect budget of exactly
    # one window is spent by the time the 504 lands and the collect attempt it
    # was sized for never starts. It has to be strictly more than one window,
    # with room for a collect attempt of its own.
    deadline_window = MODEL_RUN_TIMEOUT.read
    assert deadline_window == 600.0
    assert DEFAULT_RETRY.collect_max_elapsed >= 2 * deadline_window

    # And the fast classes do not pay for it: a refused connection still gives
    # up in a minute rather than sitting on a caller's thread for twenty.
    assert DEFAULT_RETRY.max_elapsed == 60.0


def test_the_collect_budget_applies_only_to_the_collect_class() -> None:
    # Two budgets, one origin. The same `Retrier` answers a collectable failure
    # against the long budget and everything else against the short one, so the
    # collect loop's room is not a policy-wide regression for the fast classes.
    policy = RetryPolicy(max_elapsed=10.0, collect_max_elapsed=100.0, jitter=False)
    collectable = DeadlineExceeded("still generating", http_status=504, retry_after=5)
    refused = httpx.ConnectError("refused")

    clock = _FakeClock()
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    # 40s in: past `max_elapsed`, well inside `collect_max_elapsed`.
    clock.advance(40.0)
    assert retrier.delay_before_retry(collectable) == 5.0
    assert retrier.delay_before_retry(refused) is None


def test_a_deadline_504_at_the_server_bound_still_gets_a_collect_attempt() -> None:
    # The regression the split budget exists for, end to end against a fake
    # clock: the 504 arrives after a full ten-minute attempt, which a
    # one-window budget would have spent entirely. The collect attempt has to
    # start anyway, because that generation is running and billed either way.
    clock = _FakeClock()
    retrier = Retrier(DEFAULT_RETRY, now=clock, rng=lambda: 1.0)
    clock.advance(600.0)  # one full server deadline window; see the test above
    failure = DeadlineExceeded("still generating", http_status=504, retry_after=2)
    assert retrier.delay_before_retry(failure) == 2.0


@pytest.mark.parametrize(
    "exc,expected",
    [
        (DeadlineExceeded("deadline", http_status=504, retry_after=3), True),
        (DeadlineExceeded("deadline", http_status=504), False),
        (ProviderTimeout("upstream", http_status=504, retry_after=3), False),
        (RouterError("in progress", http_status=409, retry_after=3), False),
        (
            ApiError(
                "in progress",
                code="generation_in_progress",
                http_status=409,
                retry_after=3,
            ),
            True,
        ),
        # `spec/openapi.yaml` gives its `hash_mismatch` 409 a `Retry-After`, and
        # it is still a deterministic refusal: the pace does not make asking
        # again change the answer. The bucket gate is what keeps it one.
        (ApiError("bad hash", code="hash_mismatch", http_status=409, retry_after=3), False),
        (RouterError("conflict", http_status=409), False),
        (ServiceUnavailable("later", http_status=503, retry_after=3), False),
        (InternalError("boom", http_status=500, retry_after=3), False),
        (httpx.ConnectError("refused"), False),
    ],
    ids=lambda v: str(v),
)
def test_what_counts_as_collectable(exc: BaseException, expected: bool) -> None:
    assert is_collectable(exc) is expected
    # And the default policy acts on exactly that, without the opt-in.
    if expected:
        assert DEFAULT_RETRY.should_retry(exc)


def test_the_bucket_is_read_from_either_layers_name_for_it() -> None:
    # A typed router error carries `error_type`; the protocol `ApiError` that
    # `POST /models/run` raises today names the same thing `code`. Reading only
    # the first would make the whole collect rule unreachable on the route it
    # was written for.
    assert error_bucket_of(DeadlineExceeded("deadline", http_status=504)) == "deadline_exceeded"
    assert error_bucket_of(ApiError("deadline", code="deadline_exceeded", http_status=504)) == (
        "deadline_exceeded"
    )
    assert error_bucket_of(ApiError("boom", http_status=500)) == "error"
    assert error_bucket_of(httpx.ConnectError("refused")) is None


def test_a_deadline_504_raised_as_a_protocol_error_is_still_collected() -> None:
    # The wire test above proves it end to end; this pins the reason, which is
    # that the bucket survives the layer boundary. `_CANDIDATE_FAILURES` exists
    # for the same class of silent no-op.
    exc = ApiError("deadline", code="deadline_exceeded", http_status=504, retry_after=2)
    assert DEFAULT_RETRY.should_retry(exc)


# --- the gates the collect loop is only safe behind ---


def test_a_zero_retry_after_does_not_become_a_zero_delay_resend_loop() -> None:
    # `RouterRetryAfterHeader` is pinned to `minimum: 1`, so `Retry-After: 0` is
    # a server or intermediary answering outside its own contract -- and taking
    # it verbatim would resend a full model-run POST with no wait at all, for
    # the whole budget, entirely under that server's control. Zero names no
    # usable pace: the local backoff schedule answers instead.
    clock = _FakeClock()
    policy = RetryPolicy(initial_backoff=1.0, max_backoff=8.0, jitter=False)
    retrier = Retrier(policy, now=clock, rng=lambda: 1.0)
    failure = DeadlineExceeded("still generating", http_status=504, retry_after=0)
    delays = [retrier.delay_before_retry(failure) for _ in range(3)]
    assert delays == [1.0, 2.0, 4.0]


def test_a_zero_retry_after_still_counts_as_the_server_holding_a_handle() -> None:
    # The value is unusable; the header's *presence* is not. It is the router
    # saying it holds a generation to collect, which a nonsense number does not
    # retract -- so the response stays collectable and only its pace is ignored.
    failure = DeadlineExceeded("still generating", http_status=504, retry_after=0)
    assert is_collectable(failure)
    assert DEFAULT_RETRY.should_retry(failure)


def test_turning_the_collect_loop_off_holds_even_with_the_in_flight_opt_in() -> None:
    # `retry_collectable=False` has to be an answer, not a fall-through. A
    # paced `deadline_exceeded` 504 is also a 5xx, so classified the other way
    # round it would drop into the unknown-outcome branch and be retried anyway
    # under `retry_possibly_in_flight=True` -- an opt-out that silently did
    # nothing for the one status it is named for.
    policy = RetryPolicy(retry_collectable=False, retry_possibly_in_flight=True)
    failure = DeadlineExceeded("still generating", http_status=504, retry_after=2)
    assert is_collectable(failure)
    assert not policy.should_retry(failure)
    # And the 409 half of the class, which the fall-through happened to get
    # right (a 4xx has no unknown-outcome branch to fall into) -- pinned so it
    # stays right for the stated reason rather than by accident.
    in_progress = ApiError(
        "in progress", code="generation_in_progress", http_status=409, retry_after=2
    )
    assert is_collectable(in_progress)
    assert not policy.should_retry(in_progress)
    # The rest of the policy is untouched: this is one knob, not a kill switch.
    assert policy.should_retry(InternalError("boom", http_status=500))


def test_a_paced_hash_mismatch_409_is_still_a_refusal(server) -> None:
    # The 409 the vendored spec actually documents, and it documents a
    # `Retry-After` on it. It is a deterministic refusal all the same: the
    # client-computed hash will not match on the second ask either. Without the
    # bucket gate the collect loop would resend it for the whole budget -- and
    # under the v2 rule that releases the key for a 4xx that started no work,
    # each resend is a fresh billed dispatch rather than a collection.
    server.state.model_run_error = (409, "hash_mismatch")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1
    assert excinfo.value.http_status == 409


# --- the wire shape a real Router failure arrives in ---


def test_a_deadline_504_in_routers_own_error_shape_is_still_collected(server) -> None:
    # The model-run route is served by Router, whose error body is
    # `{detail, error_type}` with the bucket repeated on `X-Comfy-Error-Type` --
    # not the v2 `{error: {code}}` envelope. Reading only the envelope would
    # collapse every real 504 to the status-derived default, and the whole
    # bucket-keyed collect rule would be a silent no-op in production while
    # every test above passed.
    server.state.model_run_router_error_shape = True
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_transient_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    server.state.model_run_fail_times = 1
    with Comfy(retry=FAST) as client:
        assert client.models.run(MODEL, ARGS) == server.state.model_run_result
    assert server.state.model_run_count == 2
    first, second = server.state.model_run_idempotency_keys
    assert first is not None
    assert second == first


def test_the_bucket_gate_holds_in_routers_own_error_shape_too(server) -> None:
    # The gate has to survive the shape change in both directions: reading the
    # header must not turn every Router 504 into a collectable one.
    server.state.model_run_router_error_shape = True
    server.state.model_run_error = (504, "provider_timeout")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=FAST) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1
    assert excinfo.value.http_status == 504


def test_a_router_error_body_keeps_its_detail_as_the_message(server) -> None:
    # Router names its human-readable string `detail`; dropping it for a bare
    # "HTTP 504" would make the shape that reaches real callers the least
    # diagnosable one.
    server.state.model_run_router_error_shape = True
    server.state.model_run_error = (500, "internal_error")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert "internal_error" in str(excinfo.value)


# --- the deployment that does NOT collect ---


def test_the_default_collect_loop_against_a_non_collecting_deployment(server) -> None:
    # The cost of defaulting this on, asserted rather than assumed. The collect
    # rule is `spec/router-openapi.yaml`'s, so it binds Comfy Router -- but
    # `COMFY_ROUTER_BASE_URL` can name a deployment that applies the v2 rule
    # instead: key stays claimed across an unknown-outcome 5xx, and the resend
    # is rejected. The caller then sees `422
    # idempotency_key_reuse` in place of the real 504. That is the trade the
    # default makes; `retry_collectable=False` is the way out of it, and this
    # pins both halves so neither can change silently.
    server.state.model_run_collects_after_deadline = False
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    with Comfy(retry=FAST) as client:
        with pytest.raises(IdempotencyKeyReuse):
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 2

    server.state.model_run_count = 0
    server.state.model_run_idempotency.clear()
    policy = RetryPolicy(
        max_elapsed=5.0, initial_backoff=0.01, max_backoff=0.02, retry_collectable=False
    )
    with Comfy(retry=policy) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert server.state.model_run_count == 1
    assert excinfo.value.http_status == 504


# --- the body snapshot the same-key rule rests on ---


def test_the_body_is_snapshotted_deeply_before_the_first_attempt(server) -> None:
    # Same key, same body is what makes the resend a collection. A shallow copy
    # leaves nested values shared with the caller, so a mutation during the
    # retry window -- up to twenty minutes on the collect path -- would send a
    # different body under the one key and earn the 422 the snapshot exists to
    # prevent.
    server.state.model_run_collects_after_deadline = True
    server.state.model_run_transient_error = (504, "deadline_exceeded")
    server.state.model_run_retry_after = "0"
    server.state.model_run_fail_times = 1
    arguments: dict[str, Any] = {"prompt": "a cat", "config": {"steps": 4}}

    class _MutatingLow:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def post_model_run(self, model: str, args: Mapping[str, Any], **kw: Any) -> Any:
            try:
                return self._inner.post_model_run(model, args, **kw)
            except BaseException:
                # The caller mutating its own nested dict *between* attempts,
                # which is the only window that matters: the collect resend is
                # what has to carry the same body as the first attempt.
                arguments["config"]["steps"] = 999
                raise

    with Comfy(retry=FAST) as client:
        models = Models(cast(Any, _MutatingLow(client._low)), FAST)
        assert models.run(MODEL, arguments) == server.state.model_run_result
    assert server.state.model_run_count == 2
    # The body is the model's native input, unwrapped — the nested value the
    # caller mutated between attempts is *not* what the resend carried.
    assert server.state.last_model_run_body["config"] == {"steps": 4}
