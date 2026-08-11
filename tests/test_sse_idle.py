"""events() must recover from a silently-stalled ("zombie") SSE connection
instead of blocking forever — a read-idle timeout trips, and the poll fallback
takes over. Regression for the long-job SSE hang."""

from __future__ import annotations

import time

import httpx

from comfy_low import transport
from comfy_sdk import AsyncComfy, Comfy, Progress, StatusChange


def _wf(client):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_events_recovers_from_zombie_stream(server, monkeypatch) -> None:
    # Short read-idle timeout so the stalled stream trips it quickly.
    monkeypatch.setattr(transport, "_SSE_IDLE_TIMEOUT", httpx.Timeout(5.0, read=0.3))
    server.state.sse_mode = "stall"
    # Socket held open + silent, well past the 0.3s read timeout.
    server.state.stall_seconds = 3.0
    server.state.polls_to_succeed = 1  # poll fallback sees terminal immediately

    with Comfy() as client:
        job = client.submit(_wf(client))
        t0 = time.monotonic()
        seen = list(job.events())  # MUST NOT hang
        elapsed = time.monotonic() - t0

    kinds = [type(e).__name__ for e in seen]
    # Delivered the pre-stall frames, then recovered to a terminal StatusChange via poll.
    assert any(isinstance(e, Progress) for e in seen), kinds
    assert isinstance(seen[-1], StatusChange) and seen[-1].status == "succeeded", kinds
    # Recovery must happen around the ~0.3s read timeout, NOT after the full 3s
    # stall (which would mean it waited for the socket to close instead of timing out).
    assert elapsed < 2.0, f"events() did not time out on the idle stream ({elapsed:.2f}s)"


async def test_async_events_recovers_from_zombie_stream(server, monkeypatch) -> None:
    # The async path uses the same idle timeout + poll fallback — must also recover.
    monkeypatch.setattr(transport, "_SSE_IDLE_TIMEOUT", httpx.Timeout(5.0, read=0.3))
    server.state.sse_mode = "stall"
    server.state.stall_seconds = 3.0
    server.state.polls_to_succeed = 1

    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        t0 = time.monotonic()
        seen = [e async for e in job.events()]  # MUST NOT hang
        elapsed = time.monotonic() - t0

    kinds = [type(e).__name__ for e in seen]
    assert any(isinstance(e, Progress) for e in seen), kinds
    assert isinstance(seen[-1], StatusChange) and seen[-1].status == "succeeded", kinds
    assert elapsed < 2.0, f"async events() did not time out on the idle stream ({elapsed:.2f}s)"


def test_get_job_events_timeout_none_opts_out_of_idle_timeout(server, monkeypatch) -> None:
    # The documented escape hatch: timeout=None disables the idle timeout, so the
    # raw iterator rides out the stall until the socket closes rather than raising.
    monkeypatch.setattr(transport, "_SSE_IDLE_TIMEOUT", httpx.Timeout(5.0, read=0.05))
    server.state.sse_mode = "stall"
    server.state.stall_seconds = 0.8  # held open, then the stub closes

    with Comfy() as client:
        job = client.submit(_wf(client))
        events_url = job._model.urls.events
        t0 = time.monotonic()
        raws = list(client._low.get_job_events(events_url, timeout=None))
        elapsed = time.monotonic() - t0

    # Rode out the full stall (until close) instead of tripping the 0.05s default —
    # proves timeout=None disabled the idle timeout. And it delivered both frames.
    assert elapsed >= 0.7, f"timeout=None should not have timed out early ({elapsed:.2f}s)"
    assert len(raws) == 2, raws
