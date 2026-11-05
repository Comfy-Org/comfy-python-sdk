"""Typed SSE events and live reconnect-with-no-replay."""

from __future__ import annotations

import pytest

from comfy_sdk import AsyncComfy, Comfy, OutputReady, Progress, StatusChange
from comfy_sdk.exceptions import Forbidden, NotFound, Unauthorized


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


# --- the terminal `error` frame ---
#
# The server ends the stream with `event: error` and an error envelope when it
# stops streaming for a reason other than the job finishing. No status frame
# follows, so the iterator must raise it rather than poll and reconnect — for
# `job_not_found` while `GET /jobs/{id}` still answers, a reconnect is an
# unbounded loop.
_SSE_ERROR_CASES = [
    ("forbidden", Forbidden),
    ("job_not_found", NotFound),
    ("credential_expired", Unauthorized),
]


@pytest.mark.parametrize(("code", "cls"), _SSE_ERROR_CASES)
def test_events_raise_typed_error_on_terminal_error_frame(server, code: str, cls: type) -> None:
    server.state.sse_error_frame_code = code
    server.state.polls_to_succeed = 1000  # a poll would report "running" and reconnect
    seen: list = []
    with Comfy() as client:
        job = client.submit(_wf(client))
        poll_before = server.state.job_poll_count
        with pytest.raises(cls) as excinfo:
            for ev in job.events():
                seen.append(ev)
                if len(seen) > 3:  # the pre-fix reconnect loop never ends
                    break

    assert seen == [StatusChange(status="running")]
    assert server.state.events_connect_count == 1  # no reconnect
    assert server.state.job_poll_count == poll_before  # no poll backstop either
    assert excinfo.value.code == code
    assert str(excinfo.value) == "Stream ended"


@pytest.mark.parametrize(("code", "cls"), _SSE_ERROR_CASES)
async def test_async_events_raise_typed_error_on_terminal_error_frame(
    server, code: str, cls: type
) -> None:
    server.state.sse_error_frame_code = code
    server.state.polls_to_succeed = 1000
    seen: list = []
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        poll_before = server.state.job_poll_count
        with pytest.raises(cls) as excinfo:
            async for ev in job.events():
                seen.append(ev)
                if len(seen) > 3:  # the pre-fix reconnect loop never ends
                    break

    assert seen == [StatusChange(status="running")]
    assert server.state.events_connect_count == 1
    assert server.state.job_poll_count == poll_before
    assert excinfo.value.code == code
    assert str(excinfo.value) == "Stream ended"


# An `error` frame whose code is not one of the documented terminal ones is not
# known to end the job's stream for good, so it is left to the poll backstop:
# the job finishing on that poll ends the iteration normally, with no raise.


def test_events_unknown_error_frame_falls_back_to_poll(server) -> None:
    server.state.sse_error_frame_code = "stream_draining"
    server.state.polls_to_succeed = 1
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    assert seen[0] == StatusChange(status="running")
    assert seen[-1] == StatusChange(status="succeeded")
    assert server.state.job_poll_count >= 1


async def test_async_events_unknown_error_frame_falls_back_to_poll(server) -> None:
    server.state.sse_error_frame_code = "stream_draining"
    server.state.polls_to_succeed = 1
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [ev async for ev in job.events()]

    assert seen[0] == StatusChange(status="running")
    assert seen[-1] == StatusChange(status="succeeded")
    assert server.state.job_poll_count >= 1
