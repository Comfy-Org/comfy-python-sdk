"""Only the first byte order mark of each physical SSE stream is ignored."""

from __future__ import annotations

import pytest

from comfy_low import AsyncComfyLow, ComfyLow
from comfy_low.sse import RawEvent, SSEDecoder
from comfy_sdk import AsyncComfy, Comfy, Progress, StatusChange

_STATUS = RawEvent(event="status", data={"status": "running"})
_MESSAGE = RawEvent(event="message", data={"status": "running"})
_DATA = 'data: {"status": "running"}'


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param(["\ufeffevent: status", _DATA, ""], [_STATUS], id="event-first"),
        pytest.param(["\ufeff" + _DATA, "event: status", ""], [_STATUS], id="data-first"),
        pytest.param(["\ufeff: heartbeat", "event: status", _DATA, ""], [_STATUS], id="comment"),
        pytest.param(["\ufeff", "event: status", _DATA, ""], [_STATUS], id="blank"),
        pytest.param(["", "\ufeffevent: status", _DATA, ""], [_MESSAGE], id="after-blank"),
        pytest.param(
            [": heartbeat", "\ufeffevent: status", _DATA, ""], [_MESSAGE], id="after-comment"
        ),
        pytest.param(["\ufeff\ufeffevent: status", _DATA, ""], [_MESSAGE], id="second-bom"),
        pytest.param(["\ufeff", "\ufeffdata: 42", ""], [], id="after-bom-blank"),
        pytest.param(
            ["data: \ufeff42", ""],
            [RawEvent(event="message", data={"raw": "\ufeff42"})],
            id="data-value",
        ),
        pytest.param(
            ['data: {"value": "\ufeff"}', ""],
            [RawEvent(event="message", data={"value": "\ufeff"})],
            id="json-value",
        ),
        pytest.param(
            ["\ufeffevent: status", _DATA, "", "\ufeffevent: status", _DATA, ""],
            [_STATUS, _MESSAGE],
            id="second-event",
        ),
    ],
)
def test_decoder_ignores_only_one_stream_leading_bom(
    lines: list[str], expected: list[RawEvent]
) -> None:
    decoder = SSEDecoder()
    assert [event for line in lines for event in decoder.push(line)] == expected


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"], ids=["plain", "bom"])
@pytest.mark.parametrize("data_first", [False, True], ids=["event-first", "data-first"])
def test_sync_sse_bom_across_http_chunks(server, prefix: bytes, data_first: bool) -> None:
    lines = [b"event: status", _DATA.encode()]
    if data_first:
        lines.reverse()
    server.state.sse_wire_streams = [prefix + b"\r\n".join(lines) + b"\r\n\r\n"]
    with ComfyLow(server.base_url, api_key=None) as client:
        assert list(client.get_job_events("job")) == [_STATUS]
    assert server.state.events_connect_count == 1


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"], ids=["plain", "bom"])
@pytest.mark.parametrize("data_first", [False, True], ids=["event-first", "data-first"])
async def test_async_sse_bom_across_http_chunks(server, prefix: bytes, data_first: bool) -> None:
    lines = [b"event: status", _DATA.encode()]
    if data_first:
        lines.reverse()
    server.state.sse_wire_streams = [prefix + b"\r\n".join(lines) + b"\r\n\r\n"]
    async with AsyncComfyLow(server.base_url, api_key=None) as client:
        assert [event async for event in client.get_job_events("job")] == [_STATUS]
    assert server.state.events_connect_count == 1


def _reconnecting_streams() -> list[bytes]:
    return [
        b'\xef\xbb\xbfevent: progress\r\ndata: {"value": 0.5}\r\n\r\n',
        b'\xef\xbb\xbfevent: status\r\ndata: {"status": "succeeded"}\r\n\r\n',
    ]


def _check_reconnected_events(server, seen: list) -> None:
    assert server.state.events_connect_count == 2
    # A terminal frame from the second stream ends iteration without another
    # poll. Keep the fallback finite even if a regression loses both frames.
    assert server.state.job_poll_count == 1
    assert len(seen) == 2
    assert isinstance(seen[0], Progress)
    assert seen[0].value == 0.5
    assert isinstance(seen[1], StatusChange)
    assert seen[1].status == "succeeded"


def test_sync_sse_bom_is_reset_on_reconnect(server) -> None:
    server.state.sse_wire_streams = _reconnecting_streams()
    server.state.polls_to_succeed = 2
    with Comfy() as client:
        workflow = client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        job = client.submit(workflow)
        seen = list(job.events())
    _check_reconnected_events(server, seen)


async def test_async_sse_bom_is_reset_on_reconnect(server) -> None:
    server.state.sse_wire_streams = _reconnecting_streams()
    server.state.polls_to_succeed = 2
    async with AsyncComfy() as client:
        workflow = client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        job = await client.submit(workflow)
        seen = [event async for event in job.events()]
    _check_reconnected_events(server, seen)
