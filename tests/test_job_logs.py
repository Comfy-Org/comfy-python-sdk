"""``get_job_logs``: the low layer's binding for ``GET /api/v2/jobs/{id}/logs``.

The stub server in conftest.py answers 200 with ``server.state.job_logs`` and
204 when that is ``None``.
"""

from __future__ import annotations

import pytest

from comfy_low import ApiError
from comfy_low.models import JobLogs
from comfy_sdk import AsyncComfy, Comfy

LOGS = {
    "text": "got prompt\nnode 7 failed",
    "truncated": False,
    "captured_at": "2026-11-19T20:00:00Z",
    "complete": True,
}


def test_get_job_logs_returns_the_captured_log(server) -> None:
    server.state.job_logs = LOGS
    with Comfy() as client:
        logs = client._low.get_job_logs("job_01")
    assert isinstance(logs, JobLogs)
    assert logs.text == "got prompt\nnode 7 failed"
    assert logs.complete is True
    assert server.state.job_logs_paths == ["/api/v2/jobs/job_01/logs"]


def test_get_job_logs_returns_none_for_a_job_with_no_log(server) -> None:
    server.state.job_logs_no_reason = "finished"
    with Comfy() as client:
        assert client._low.get_job_logs("job_01") is None


def test_get_job_logs_follows_the_jobs_own_logs_link(server) -> None:
    server.state.job_logs = LOGS
    with Comfy() as client:
        assert client._low.get_job_logs("/api/v2/jobs/job_01/logs") is not None
    assert server.state.job_logs_paths == ["/api/v2/jobs/job_01/logs"]


def test_get_job_logs_raises_for_a_missing_job(server) -> None:
    server.state.job_not_found = True
    with Comfy() as client, pytest.raises(ApiError) as caught:
        client._low.get_job_logs("job_01")
    assert caught.value.http_status == 404


async def test_async_get_job_logs_returns_the_log_or_none(server) -> None:
    async with AsyncComfy() as client:
        assert await client._low.get_job_logs("job_01") is None
        server.state.job_logs = LOGS
        logs = await client._low.get_job_logs("job_01")
    assert isinstance(logs, JobLogs)
    assert logs.text == "got prompt\nnode 7 failed"
