"""Low-layer decoding primitives: the SSE decoder and multipart size probe.

The suite only ever feeds the decoder well-formed JSON from the stub server, so
its comment/blank/malformed-frame branches were dark; and every upload test
passes an explicit `file_size`, so the size-probe fallback never ran.
"""

from __future__ import annotations

import io
import os

import pytest

from comfy_low._multipart import file_size_of
from comfy_low.sse import RawEvent, SSEDecoder


def _feed(decoder: SSEDecoder, *lines: str) -> list[RawEvent]:
    out: list[RawEvent] = []
    for line in lines:
        out.extend(decoder.push(line))
    return out


def test_sse_decoder_emits_one_event_on_blank_line():
    events = _feed(SSEDecoder(), "event: status", 'data: {"status": "running"}', "")
    assert events == [RawEvent(event="status", data={"status": "running"})]


def test_sse_decoder_ignores_comment_and_stray_blank_lines():
    # A leading comment and a blank line with nothing buffered both yield nothing.
    assert _feed(SSEDecoder(), ":heartbeat") == []
    assert _feed(SSEDecoder(), "") == []


def test_sse_decoder_falls_back_to_raw_on_malformed_json():
    events = _feed(SSEDecoder(), "event: log", "data: not-json{{{", "")
    assert events == [RawEvent(event="log", data={"raw": "not-json{{{"})]


def test_sse_decoder_wraps_non_object_json_in_value():
    events = _feed(SSEDecoder(), "data: 42", "")
    assert events == [RawEvent(event="message", data={"value": 42})]


def test_sse_decoder_joins_multiple_data_lines():
    events = _feed(SSEDecoder(), "data: line one", "data: line two", "")
    assert events == [RawEvent(event="message", data={"raw": "line one\nline two"})]


def test_file_size_of_seekable_stream_without_fileno():
    # A BytesIO has no OS fd, so this exercises the tell()/seek() fallback that
    # unknown-size (in-memory / pipe) uploads rely on.
    buf = io.BytesIO(b"abcdefghij")
    assert file_size_of(buf) == 10
    buf.read(4)  # partial consume -> size is the REMAINING bytes from the cursor
    assert file_size_of(buf) == 6


@pytest.mark.parametrize("buffering", [-1, 0], ids=["buffered", "raw"])
@pytest.mark.parametrize("offset", [0, 4, 10, 14])
def test_file_size_of_disk_file_reports_remaining_bytes(tmp_path, buffering, offset):
    path = tmp_path / "source.bin"
    path.write_bytes(b"abcdefghij")
    with open(path, "rb", buffering=buffering) as stream:
        # A buffered read can advance the OS fd past the logical cursor.
        stream.read(min(offset, 10))
        if offset > 10:
            stream.seek(offset)
        assert stream.tell() == offset
        assert file_size_of(stream) == max(0, 10 - offset)
        assert stream.tell() == offset
        assert stream.read() == b"abcdefghij"[offset:]


def test_file_size_of_memory_stream_beyond_eof():
    with io.BytesIO(b"abcdefghij") as stream:
        stream.seek(14)
        assert file_size_of(stream) == 0
        assert stream.tell() == 14


def test_file_size_of_pipe_is_unknown_and_does_not_consume_bytes():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as stream, os.fdopen(write_fd, "wb") as writer:
        writer.write(b"pipe-bytes")
        writer.flush()
        assert file_size_of(stream) is None
        assert stream.read(10) == b"pipe-bytes"


def test_file_size_of_unsized_source_returns_none():
    class _NoSize:
        def fileno(self):
            raise OSError("no fd")

        def tell(self):
            raise OSError("not seekable")

    assert file_size_of(_NoSize()) is None
