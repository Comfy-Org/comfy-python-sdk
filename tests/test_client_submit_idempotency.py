"""``submit()`` stamps its ``Idempotency-Key`` onto whatever it raises.

``Comfy.submit`` / ``AsyncComfy.submit`` mint a key per call and send it on
``POST /jobs``. Until now that key was a local of the submitting frame, so it
died with any exception the call raised — and the caller of an auto-minted
submit had no record of what had been sent. These tests pin the same contract
``tests/test_models_run.py`` pins for ``models.run``: every failure of the call
carries ``.idempotency_key``, including a transport failure that never reached
a response and a cancellation of an in-flight submit.

The semantics differ from ``models.run``'s and the difference is deliberate:
``POST /jobs`` *rejects* a reused key (``422 idempotency_key_reuse``) rather
than replaying it, so this key is a record of what was sent — poll or list for
the job the first attempt may have created — not a replay handle.

Everything here runs against the stubbed server in ``conftest.py``, except the
two cases a listening server cannot produce (a connect failure, a cancellation).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

import comfy_sdk.client as _client_module
from comfy_sdk import AsyncComfy, Comfy, ComfyError, IdempotencyKeyReuse, QueueFull
from comfy_sdk.client import BASE_URL_ENV_VAR

_GRAPH = {"3": {"class_type": "KSampler", "inputs": {}}}


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json(_GRAPH)


def _closed_port() -> int:
    """A port nothing is listening on — bound, read, then released.

    Released rather than held: holding the socket bound-but-not-listening
    would close the tiny window in which something else could take the port,
    but a SYN to such a port is *dropped* on macOS rather than refused, so the
    connect runs to ``ConnectTimeout`` instead of the ``ConnectError`` these
    tests are about. The window between release and connect is the price.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StepClock:
    """A monotonic clock that advances a fixed step per read.

    The queue-full budget is spent by *reads* rather than by wall time, so
    "the budget ran out after N attempts" is exact instead of a race against a
    loaded machine.
    """

    def __init__(self, step: float = 1.0) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._t
        self._t += self._step
        return now


# --- the auto-minted key, which the caller never saw ----------------------


def test_a_failed_submit_exposes_the_exact_auto_minted_key(server) -> None:
    # An unmapped code, so this lands on the base `ComfyError` — the stamp has
    # to reach it, not only the typed subclasses.
    server.state.job_error = (422, "validation_error")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.submit(_wf(client))
    (sent,) = server.state.jobs_idempotency_keys
    assert sent is not None
    # The exact key the server received, not merely "a key": truthiness would
    # pass on a freshly minted one, which records nothing about what was sent.
    assert excinfo.value.idempotency_key == sent


async def test_a_failed_async_submit_exposes_the_exact_auto_minted_key(server) -> None:
    server.state.job_error = (422, "validation_error")
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.submit(_wf(client))
    (sent,) = server.state.jobs_idempotency_keys
    assert sent is not None
    assert excinfo.value.idempotency_key == sent


def test_a_caller_supplied_key_round_trips_onto_the_exception(server) -> None:
    server.state.job_error = (422, "validation_error")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.submit(_wf(client), idempotency_key="abc")
    assert excinfo.value.idempotency_key == "abc"
    assert server.state.jobs_idempotency_keys == ["abc"]


async def test_a_caller_supplied_key_round_trips_on_the_async_client(server) -> None:
    server.state.job_error = (422, "validation_error")
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.submit(_wf(client), idempotency_key="abc-async")
    assert excinfo.value.idempotency_key == "abc-async"
    assert server.state.jobs_idempotency_keys == ["abc-async"]


def test_an_empty_caller_key_is_refused_before_any_request(server) -> None:
    # `""` used to fall into the mint-a-fresh-key branch: the caller's dedup
    # silently disabled, and the exception then reporting a key the caller
    # never passed. Validated like `models.run`'s key, before any bytes move.
    with Comfy() as client:
        with pytest.raises(ValueError, match="must not be empty"):
            client.submit(_wf(client), idempotency_key="")
    assert server.state.jobs_idempotency_keys == []


async def test_an_invalid_caller_key_is_refused_before_any_async_request(server) -> None:
    async with AsyncComfy() as client:
        with pytest.raises(ValueError, match="must not be empty"):
            await client.submit(_wf(client), idempotency_key="")
        with pytest.raises(ValueError, match="printable ASCII"):
            await client.submit(_wf(client), idempotency_key="bad\r\nkey")
    assert server.state.jobs_idempotency_keys == []


# --- the retried paths: one key across every attempt ----------------------


def test_an_exhausted_queue_full_budget_carries_the_retried_key(server, monkeypatch) -> None:
    # The server never clears backpressure, so the retry loop runs to the end
    # of its budget and surfaces `QueueFull`. That exception has to carry the
    # key — and it must be the *same* key every attempt was made under, not a
    # fresh one minted somewhere along the way.
    server.state.queue_full_times = 1_000_000
    monkeypatch.setattr(_client_module, "_now", _StepClock())
    monkeypatch.setattr(_client_module, "_QUEUE_RETRY_BUDGET", 3.0)
    with Comfy() as client:
        with pytest.raises(QueueFull) as excinfo:
            client.submit(_wf(client))
    sent = server.state.jobs_idempotency_keys
    assert len(sent) > 1
    assert set(sent) == {excinfo.value.idempotency_key}
    assert excinfo.value.idempotency_key is not None


async def test_an_exhausted_async_queue_full_budget_carries_the_retried_key(
    server, monkeypatch
) -> None:
    server.state.queue_full_times = 1_000_000
    monkeypatch.setattr(_client_module, "_now", _StepClock())
    monkeypatch.setattr(_client_module, "_QUEUE_RETRY_BUDGET", 3.0)
    async with AsyncComfy() as client:
        with pytest.raises(QueueFull) as excinfo:
            await client.submit(_wf(client))
    sent = server.state.jobs_idempotency_keys
    assert len(sent) > 1
    assert set(sent) == {excinfo.value.idempotency_key}
    assert excinfo.value.idempotency_key is not None


# --- reject-not-replay: the key that was refused --------------------------


def test_a_rejected_reused_key_is_the_key_on_the_exception(server) -> None:
    # `POST /jobs` keys are single-use: the second call under the same key is
    # refused rather than replayed. The exception names the key that was
    # rejected, which is what a caller polls or lists for the first job by.
    with Comfy() as client:
        client.submit(_wf(client), idempotency_key="reused-01")
        with pytest.raises(IdempotencyKeyReuse) as excinfo:
            client.submit(_wf(client), idempotency_key="reused-01")
    assert excinfo.value.idempotency_key == "reused-01"
    assert server.state.jobs_idempotency_keys == ["reused-01", "reused-01"]


async def test_a_rejected_reused_key_is_the_key_on_the_async_exception(server) -> None:
    async with AsyncComfy() as client:
        await client.submit(_wf(client), idempotency_key="reused-02")
        with pytest.raises(IdempotencyKeyReuse) as excinfo:
            await client.submit(_wf(client), idempotency_key="reused-02")
    assert excinfo.value.idempotency_key == "reused-02"


# --- no response at all: the case the key is most needed on ---------------


def test_a_transport_level_failure_carries_the_key(monkeypatch) -> None:
    # Nothing to translate — no response reached the client — so before this
    # the httpx error escaped `submit()` untouched and the caller was left with
    # a request that may or may not have created a job and no key to look for
    # it by.
    monkeypatch.setenv(BASE_URL_ENV_VAR, f"http://127.0.0.1:{_closed_port()}")
    with Comfy() as client:
        with pytest.raises(httpx.ConnectError) as excinfo:
            client.submit(_wf(client))
    assert excinfo.value.idempotency_key is not None  # type: ignore[attr-defined]
    # The documented trio has to *read* rather than raise on exactly the
    # failures it is most needed on: httpx's classes declare neither attribute.
    assert excinfo.value.request_id is None  # type: ignore[attr-defined]
    assert excinfo.value.retry_after is None  # type: ignore[attr-defined]


async def test_an_async_transport_level_failure_carries_the_key(monkeypatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, f"http://127.0.0.1:{_closed_port()}")
    async with AsyncComfy() as client:
        with pytest.raises(httpx.ConnectError) as excinfo:
            await client.submit(_wf(client))
    assert excinfo.value.idempotency_key is not None  # type: ignore[attr-defined]
    assert excinfo.value.request_id is None  # type: ignore[attr-defined]
    assert excinfo.value.retry_after is None  # type: ignore[attr-defined]


def test_a_caller_supplied_key_survives_a_transport_failure(monkeypatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, f"http://127.0.0.1:{_closed_port()}")
    with Comfy() as client:
        with pytest.raises(httpx.ConnectError) as excinfo:
            client.submit(_wf(client), idempotency_key="abc-transport")
    assert excinfo.value.idempotency_key == "abc-transport"  # type: ignore[attr-defined]


# --- run(), which submits through submit() --------------------------------


def test_run_surfaces_the_key_of_a_failed_submit_phase(server) -> None:
    # `run` mints no key of its own — it calls `submit`, so the stamp has to
    # reach the caller through it.
    server.state.job_error = (422, "invalid_workflow")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.run(_wf(client))
    (sent,) = server.state.jobs_idempotency_keys
    assert sent is not None
    assert excinfo.value.idempotency_key == sent


async def test_async_run_surfaces_the_key_of_a_failed_submit_phase(server) -> None:
    server.state.job_error = (422, "invalid_workflow")
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.run(_wf(client))
    (sent,) = server.state.jobs_idempotency_keys
    assert sent is not None
    assert excinfo.value.idempotency_key == sent


# --- cancellation, the one BaseException the key rides out on -------------


async def test_cancelling_an_in_flight_submit_still_yields_the_key(server) -> None:
    # A submit cancelled mid-flight — `task.cancel()` on the task awaiting it —
    # may already have reached the server and created a job, so the key is the
    # caller's only record of what to look for. The cancellation itself must
    # still propagate: it is re-raised bare, never converted. (An
    # `asyncio.wait_for` timeout is a different exit: it swallows the inner
    # cancellation and raises its own `TimeoutError`, which carries no key.)
    seen: list[str | None] = []

    async def _cancelled_post_jobs(
        graph: dict[str, Any], *, idempotency_key: str | None = None, **kw: Any
    ) -> Any:
        seen.append(idempotency_key)
        raise asyncio.CancelledError

    async with AsyncComfy() as client:
        wf = _wf(client)
        client._low.post_jobs = _cancelled_post_jobs  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await client.submit(wf)
    assert seen == [excinfo.value.idempotency_key]  # type: ignore[attr-defined]
    assert excinfo.value.idempotency_key is not None  # type: ignore[attr-defined]
    assert excinfo.value.request_id is None  # type: ignore[attr-defined]
