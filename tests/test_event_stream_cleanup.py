"""High-level event iteration owns each low-level HTTP stream."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager

import pytest

from comfy_sdk import AsyncComfy, Comfy, StatusChange


@pytest.mark.parametrize("exit_mode", ["terminal", "close"])
def test_events_close_upstream(server, monkeypatch, exit_mode):
    with Comfy() as client:
        job = client.submit(
            client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        )
        low = job._low
        original_events = low.get_job_events
        original_open = low.open
        retained = []
        responses = []
        exited = []

        def retain_events(*args, **kwargs):
            stream = original_events(*args, **kwargs)
            retained.append(stream)
            return stream

        @contextmanager
        def observe_open(*args, **kwargs):
            try:
                with original_open(*args, **kwargs) as response:
                    responses.append(response)
                    yield response
            finally:
                exited.append(True)

        monkeypatch.setattr(low, "get_job_events", retain_events)
        monkeypatch.setattr(low, "open", observe_open)
        stream = job.events()
        try:
            if exit_mode == "terminal":
                seen = list(stream)
                assert isinstance(seen[-1], StatusChange)
                assert seen[-1].status == "succeeded"
            else:
                next(stream)
                stream.close()
            assert len(retained) == 1
            assert exited == [True]
            assert responses[0].is_closed
        finally:
            stream.close()
            for upstream in retained:
                upstream.close()


@pytest.mark.parametrize("exit_mode", ["terminal", "close", "cancel"])
async def test_async_events_close_upstream(server, monkeypatch, exit_mode):
    if exit_mode == "cancel":
        server.state.sse_mode = "stall"
        server.state.stall_seconds = 0.2
    async with AsyncComfy() as client:
        job = await client.submit(
            client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        )
        low = job._low
        original_events = low.get_job_events
        original_open = low.open
        retained = []
        responses = []
        exited = []

        def retain_events(*args, **kwargs):
            stream = original_events(*args, **kwargs)
            retained.append(stream)
            return stream

        @asynccontextmanager
        async def observe_open(*args, **kwargs):
            try:
                async with original_open(*args, **kwargs) as response:
                    responses.append(response)
                    yield response
            finally:
                exited.append(True)

        monkeypatch.setattr(low, "get_job_events", retain_events)
        monkeypatch.setattr(low, "open", observe_open)
        stream = job.events()
        try:
            if exit_mode == "terminal":
                seen = [event async for event in stream]
                assert isinstance(seen[-1], StatusChange)
                assert seen[-1].status == "succeeded"
            elif exit_mode == "close":
                await anext(stream)
                await stream.aclose()
            else:
                await anext(stream)
                await anext(stream)
                pending = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert len(retained) == 1
            assert exited == [True]
            assert responses[0].is_closed
        finally:
            await stream.aclose()
            for upstream in retained:
                await upstream.aclose()
