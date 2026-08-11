"""Typed SSE events and live reconnect-with-no-replay."""

from __future__ import annotations

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
