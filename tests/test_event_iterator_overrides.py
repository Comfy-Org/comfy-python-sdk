"""Iterator-only low-level overrides stay valid when high-level events close."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from comfy_low.models import Job as LowJob
from comfy_low.sse import RawEvent
from comfy_low.transport import AsyncComfyLow, ComfyLow
from comfy_sdk import AsyncJob, Job, Progress, StatusChange


def _model(status: str = "running") -> LowJob:
    return LowJob.model_validate(
        {
            "id": "iterator-job",
            "status": status,
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": None,
            "completed_at": None,
            "expires_at": "2026-01-02T00:00:00Z",
            "queue_position": None,
            "progress": None,
            "outputs": [],
            "error": None,
            "urls": {"self": "/jobs/iterator-job", "events": "/events", "cancel": "/cancel"},
        }
    )


class _Events(Iterator[RawEvent]):
    def __init__(self, frames: list[RawEvent]) -> None:
        self.frames = iter(frames)
        self.reads = 0

    def __next__(self) -> RawEvent:
        self.reads += 1
        return next(self.frames)


class _AsyncEvents(AsyncIterator[RawEvent]):
    def __init__(self, frames: list[RawEvent]) -> None:
        self.frames = iter(frames)
        self.reads = 0

    async def __anext__(self) -> RawEvent:
        self.reads += 1
        try:
            return next(self.frames)
        except StopIteration:
            raise StopAsyncIteration from None


class _IteratorLow(ComfyLow):
    def __init__(self, events: Iterator[RawEvent]) -> None:
        super().__init__("http://iterator.invalid")
        self.events = events
        self.stream_calls = 0
        self.poll_calls = 0

    def get_job_events(self, job_id_or_url: str, *, timeout: Any = None) -> Iterator[RawEvent]:
        self.stream_calls += 1
        return self.events

    def get_job(self, job_id_or_url: str, *, timeout: Any = None) -> LowJob:
        self.poll_calls += 1
        return _model("succeeded")


class _IteratorAsyncLow(AsyncComfyLow):
    def __init__(self, events: AsyncIterator[RawEvent]) -> None:
        super().__init__("http://iterator.invalid")
        self.events = events
        self.stream_calls = 0
        self.poll_calls = 0

    def get_job_events(self, job_id_or_url: str, *, timeout: Any = None) -> AsyncIterator[RawEvent]:
        self.stream_calls += 1
        return self.events

    async def get_job(self, job_id_or_url: str, *, timeout: Any = None) -> LowJob:
        self.poll_calls += 1
        return _model("succeeded")


def _frames(exit_mode: str) -> list[RawEvent]:
    if exit_mode == "empty":
        return []
    if exit_mode == "close":
        return [RawEvent(event="progress", data={"value": 0.5})]
    return [RawEvent(event="status", data={"status": "succeeded"})]


@pytest.mark.parametrize("exit_mode", ["terminal", "close", "empty"])
@pytest.mark.parametrize("cleanup", ["absent", "noncallable", "callable"])
def test_events_accept_iterator_overrides(exit_mode, cleanup):
    raw = _Events(_frames(exit_mode))
    closed = []
    if cleanup != "absent":
        raw.close = (lambda: closed.append(True)) if cleanup == "callable" else None
    with _IteratorLow(raw) as low:
        events = Job(low, _model()).events()
        try:
            if exit_mode == "close":
                assert next(events) == Progress(value=0.5)
                events.close()
            else:
                assert list(events) == [StatusChange(status="succeeded")]
            assert low.stream_calls == 1
            assert low.poll_calls == (1 if exit_mode == "empty" else 0)
            assert raw.reads == 1
            assert closed == ([True] if cleanup == "callable" else [])
        finally:
            events.close()


@pytest.mark.parametrize("exit_mode", ["terminal", "close", "empty"])
@pytest.mark.parametrize("cleanup", ["absent", "noncallable", "callable"])
async def test_events_accept_async_iterator_overrides(exit_mode, cleanup):
    raw = _AsyncEvents(_frames(exit_mode))
    closed = []

    async def close():
        closed.append(True)

    if cleanup != "absent":
        raw.aclose = close if cleanup == "callable" else None
    async with _IteratorAsyncLow(raw) as low:
        events = AsyncJob(low, _model()).events()
        try:
            if exit_mode == "close":
                assert await anext(events) == Progress(value=0.5)
                await events.aclose()
            else:
                assert [event async for event in events] == [StatusChange(status="succeeded")]
            assert low.stream_calls == 1
            assert low.poll_calls == (1 if exit_mode == "empty" else 0)
            assert raw.reads == 1
            assert closed == ([True] if cleanup == "callable" else [])
        finally:
            await events.aclose()
