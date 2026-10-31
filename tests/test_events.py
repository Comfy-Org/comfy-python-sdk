"""Typed SSE events and live reconnect-with-no-replay."""

from __future__ import annotations

import asyncio

import pytest

import comfy_sdk.jobs as _jobs_module
from comfy_sdk import AsyncComfy, Comfy, OutputReady, Progress, StatusChange


def _wf(client: Comfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_typed_events_stream_to_terminal(server) -> None:
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    kinds = [type(e).__name__ for e in seen]
    assert "Progress" in kinds
    assert "OutputReady" in kinds
    # The stream ends on the terminal status event.
    assert isinstance(seen[-1], StatusChange)
    assert seen[-1].status == "succeeded"

    progress = [e for e in seen if isinstance(e, Progress)][0]
    assert 0.0 <= progress.value <= 1.0
    output_ready = [e for e in seen if isinstance(e, OutputReady)][0]
    assert output_ready.output.node_id == "13"


def test_sse_reconnect_with_no_replay(server) -> None:
    # First stream drops after one progress frame with no terminal; the client
    # must reconnect (fresh live frames, nothing replayed) and finish.
    server.state.sse_mode = "reconnect"
    # Keep the poll-authoritative backstop reporting "running" so the client
    # reconnects to the stream rather than short-circuiting to terminal on poll.
    server.state.polls_to_succeed = 1000
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    # Two physical connections were made to the events endpoint.
    assert server.state.events_connect_count == 2
    # Completed on a terminal status.
    assert isinstance(seen[-1], StatusChange)
    assert seen[-1].status == "succeeded"
    # No replay: the first connection's single progress frame is not duplicated
    # by a cursor-based resume. The 2nd connection sends its own fresh progress.
    progresses = [e for e in seen if isinstance(e, Progress)]
    assert len(progresses) == 2  # one per connection, not a replayed backlog


def test_events_polls_to_terminal_when_stream_ends_without_terminal(server) -> None:
    # The stream closes cleanly (no error) after one progress frame, without a
    # terminal status. The documented backstop: poll the authoritative state —
    # it already reports succeeded, so events() ends on a synthetic terminal
    # StatusChange instead of reconnecting.
    server.state.sse_mode = "reconnect"  # 1st connection: one progress frame, clean close
    server.state.polls_to_succeed = 1
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    assert server.state.events_connect_count == 1  # backstop polled; no reconnect
    assert isinstance(seen[-1], StatusChange)
    assert seen[-1].status == "succeeded"


def test_events_end_silently_when_events_endpoint_not_implemented(server) -> None:
    """Graceful degradation on a surface without SSE: a 501 from the events
    endpoint (contract-legal) ends the iteration with no events, and the
    poll-authoritative ``wait`` stays fully functional. Before this behavior
    was introduced, ``events()`` raised the protocol-level ``ApiError``
    (code ``not_implemented``, http_status 501)."""
    server.state.events_not_implemented = True
    with Comfy() as client:
        job = client.submit(_wf(client))
        assert list(job.events()) == []
        assert job.wait().status == "succeeded"

    assert server.state.events_connect_count == 1  # no reconnect loop on 501


async def test_async_events_end_silently_when_events_endpoint_not_implemented(server) -> None:
    server.state.events_not_implemented = True
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        assert [e async for e in job.events()] == []
        assert (await job.wait()).status == "succeeded"

    assert server.state.events_connect_count == 1


def test_preview_event_decodes_base64(server) -> None:
    from base64 import b64encode

    from comfy_low.sse import RawEvent
    from comfy_sdk.events import event_from_raw

    raw = RawEvent(
        event="preview",
        data={
            "node_id": "12",
            "content_type": "image/jpeg",
            "data_base64": b64encode(b"jpeg-bytes").decode(),
        },
    )
    ev = event_from_raw(raw, output_binder=lambda m: m)
    assert ev.data == b"jpeg-bytes"
    assert ev.node_id == "12"


# -- reconnect backoff on a stream that keeps dropping -----------------------

_DROP_PAUSES = [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]


class _StopLoop(Exception):
    """Aborts the otherwise endless reconnect loop once enough pauses are seen."""


def _recording_sleep(pauses: list[float], limit: int):
    def fake_sleep(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) >= limit:
            raise _StopLoop

    return fake_sleep


def _drop_always(server) -> None:
    # Every connect answers 200 text/event-stream and closes with no frames,
    # while the poll backstop keeps reporting a non-terminal job.
    server.state.sse_mode = "drop"
    server.state.polls_to_succeed = 1000


def test_events_reconnect_pause_backs_off_on_empty_drops(server, monkeypatch) -> None:
    _drop_always(server)
    pauses: list[float] = []
    monkeypatch.setattr(_jobs_module.time, "sleep", _recording_sleep(pauses, len(_DROP_PAUSES)))
    with Comfy() as client:
        job = client.submit(_wf(client))
        polls_before = server.state.job_poll_count
        with pytest.raises(_StopLoop):
            list(job.events())

    assert pauses == pytest.approx(_DROP_PAUSES)
    assert server.state.events_connect_count <= len(pauses) + 1
    assert server.state.job_poll_count - polls_before <= len(pauses) + 1


async def test_async_events_reconnect_pause_backs_off_on_empty_drops(server, monkeypatch) -> None:
    _drop_always(server)
    pauses: list[float] = []
    record = _recording_sleep(pauses, len(_DROP_PAUSES))
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        record(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        polls_before = server.state.job_poll_count
        with pytest.raises(_StopLoop):
            [e async for e in job.events()]

    assert pauses == pytest.approx(_DROP_PAUSES)
    assert server.state.events_connect_count <= len(pauses) + 1
    assert server.state.job_poll_count - polls_before <= len(pauses) + 1


def test_events_reconnect_pause_backs_off_on_snapshot_then_close(server, monkeypatch) -> None:
    # Every connect delivers the on-connect status/progress snapshot and then
    # closes: frames arrived, but the stream is no healthier than an empty one,
    # so the pause must keep backing off rather than resetting each time.
    _drop_always(server)
    server.state.sse_drop_snapshot = True
    pauses: list[float] = []
    monkeypatch.setattr(_jobs_module.time, "sleep", _recording_sleep(pauses, len(_DROP_PAUSES)))
    with Comfy() as client:
        job = client.submit(_wf(client))
        polls_before = server.state.job_poll_count
        with pytest.raises(_StopLoop):
            list(job.events())

    assert pauses == pytest.approx(_DROP_PAUSES)
    assert server.state.events_connect_count <= len(pauses) + 1
    assert server.state.job_poll_count - polls_before <= len(pauses) + 1


async def test_async_events_reconnect_pause_backs_off_on_snapshot_then_close(
    server, monkeypatch
) -> None:
    _drop_always(server)
    server.state.sse_drop_snapshot = True
    pauses: list[float] = []
    record = _recording_sleep(pauses, len(_DROP_PAUSES))
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        record(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        with pytest.raises(_StopLoop):
            [e async for e in job.events()]

    assert pauses == pytest.approx(_DROP_PAUSES)


def test_events_reconnect_pause_resets_after_a_long_lived_connection(server, monkeypatch) -> None:
    # Three short snapshot-then-close connects back the pause off; the 4th
    # stays open past the healthy threshold (no terminal) so the next pause
    # starts over at 0.1.
    _drop_always(server)
    server.state.sse_drop_snapshot = True
    server.state.sse_hold_on_connect = 4
    server.state.sse_hold_seconds = 0.5
    monkeypatch.setattr(_jobs_module, "_HEALTHY_STREAM_SECONDS", 0.3)
    pauses: list[float] = []
    monkeypatch.setattr(_jobs_module.time, "sleep", _recording_sleep(pauses, 6))
    with Comfy() as client:
        job = client.submit(_wf(client))
        with pytest.raises(_StopLoop):
            list(job.events())

    assert pauses == pytest.approx([0.1, 0.2, 0.4, 0.1, 0.2, 0.4])
