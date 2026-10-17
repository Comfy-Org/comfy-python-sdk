"""Sans-IO Server-Sent-Events decoding.

The decoder is pure and line-driven: feed it decoded text lines one at a time and
it yields complete events. Both the sync and async transports drive the same
decoder, so the wire-format parsing lives in exactly one place (the IO — reading
lines off a sync or async body — is the only thing that differs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawEvent:
    """One decoded SSE frame. ``event`` defaults to ``message`` per the spec."""

    event: str
    data: dict[str, Any]


class SSEDecoder:
    """Incremental SSE parser. Call :meth:`push` per line; it returns any
    completed events (usually zero or one). A blank line dispatches the buffer.
    """

    def __init__(self) -> None:
        self._at_start = True
        self._event: str | None = None
        self._data: list[str] = []

    def push(self, line: str) -> list[RawEvent]:
        # SSE's UTF-8 decoding strips exactly one BOM at the start of a stream.
        if self._at_start:
            self._at_start = False
            line = line.removeprefix("\ufeff")
        # Strip a single trailing newline; callers may or may not have kept it.
        line = line.rstrip("\r\n")

        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            # Comment / keep-alive heartbeat — ignore.
            return []

        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        # `id` and `retry` are intentionally ignored: this stream carries no
        # cursor (no Last-Event-ID resume), by contract.
        return []

    def _dispatch(self) -> list[RawEvent]:
        if not self._data and self._event is None:
            return []
        raw = "\n".join(self._data)
        event = self._event or "message"
        self._event = None
        self._data = []
        if raw == "":
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw}
        if not isinstance(data, dict):
            data = {"value": data}
        return [RawEvent(event=event, data=data)]
