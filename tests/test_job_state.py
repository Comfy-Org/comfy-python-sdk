"""The state a job handle exposes beyond id/status/outputs/error.

Timestamps, progress, queue position, metrics and follow-up links are views
onto the model the handle already holds: each reads what the last fetch
returned and never re-fetches. Each one is covered populated and ``None`` (where
the wire field is nullable), on both :class:`Job` and :class:`AsyncJob`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from conftest import _job_json

from comfy_low import AsyncComfyLow, ComfyLow
from comfy_low.models import Job as LowJob
from comfy_low.models import JobUrls
from comfy_sdk import AsyncComfy, Comfy, Progress
from comfy_sdk.jobs import AsyncJob, Job

_UNUSED = "http://example.invalid"

_PROGRESS = {
    "value": 0.42,
    "nodes_done": 11,
    "nodes_total": 31,
    "current_node": "12",
    "current_node_class": "KSampler",
    "step": 21,
    "steps": 50,
    "message": "KSampler 21/50",
}

_POPULATED = {
    "created_at": "2026-07-10T18:20:00Z",
    "started_at": "2026-07-10T18:20:09Z",
    "completed_at": "2026-07-10T18:20:51.5Z",
    "expires_at": "2026-07-11T18:20:00Z",
    "queue_position": 3,
    "progress": _PROGRESS,
    "metrics": {"queue_ms": 9000, "execution_ms": 42500},
}

_NULLED = {
    "started_at": None,
    "completed_at": None,
    "queue_position": None,
    "progress": None,
}


def _model(**overrides: Any) -> LowJob:
    body = _job_json("job_01", "succeeded")
    body.update(overrides)
    if "metrics" in overrides and overrides["metrics"] is None:
        del body["metrics"]  # optional on the wire: absent rather than null
    return LowJob.model_validate(body)


@pytest.fixture(params=["sync", "async"])
def handle(request):
    """Build a handle of either class around a model; nothing here hits the network."""

    def build(**overrides: Any) -> Job | AsyncJob:
        model = _model(**overrides)
        if request.param == "sync":
            return Job(ComfyLow(_UNUSED), model)
        return AsyncJob(AsyncComfyLow(_UNUSED), model)

    return build


def test_timestamps_are_aware_datetimes(handle) -> None:
    job = handle(**_POPULATED)

    assert job.created_at == datetime(2026, 7, 10, 18, 20, tzinfo=timezone.utc)
    assert job.started_at == datetime(2026, 7, 10, 18, 20, 9, tzinfo=timezone.utc)
    assert job.completed_at == datetime(2026, 7, 10, 18, 20, 51, 500000, tzinfo=timezone.utc)
    assert job.expires_at == datetime(2026, 7, 11, 18, 20, tzinfo=timezone.utc)


def test_duration_is_completed_at_minus_started_at(handle) -> None:
    job = handle(**_POPULATED)

    assert job.completed_at - job.started_at == timedelta(seconds=42.5)


def test_unset_timestamps_are_none(handle) -> None:
    job = handle(**_NULLED)

    assert job.started_at is None
    assert job.completed_at is None
    # The two non-nullable ones are still there.
    assert isinstance(job.created_at, datetime)
    assert isinstance(job.expires_at, datetime)


def test_progress_is_the_type_the_event_stream_yields(handle) -> None:
    job = handle(**_POPULATED)

    assert job.progress == Progress(
        value=0.42,
        message="KSampler 21/50",
        nodes_done=11,
        nodes_total=31,
        current_node="12",
        step=21,
        steps=50,
    )


def test_progress_is_none_when_the_job_reports_none(handle) -> None:
    assert handle(**_NULLED).progress is None


def test_queue_position(handle) -> None:
    assert handle(**_POPULATED).queue_position == 3
    assert handle(queue_position=0).queue_position == 0
    assert handle(**_NULLED).queue_position is None


def test_metrics_pass_through_including_a_null_value(handle) -> None:
    assert handle(**_POPULATED).metrics == {"queue_ms": 9000, "execution_ms": 42500}
    assert handle().metrics == {"queue_ms": 9000, "execution_ms": None}


def test_metrics_are_none_when_absent(handle) -> None:
    assert handle(metrics=None).metrics is None


def test_metrics_are_a_copy(handle) -> None:
    job = handle(**_POPULATED)
    job.metrics["queue_ms"] = 0

    assert job.metrics == {"queue_ms": 9000, "execution_ms": 42500}


def test_urls_pass_through(handle) -> None:
    job = handle()

    assert isinstance(job.urls, JobUrls)
    assert job.urls.self == "/api/v2/jobs/job_01"
    assert job.urls.events == "/api/v2/jobs/job_01/events"
    assert job.urls.cancel == "/api/v2/jobs/job_01/cancel"
    assert job.urls.logs is None


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_a_fetched_job_exposes_its_state(server) -> None:
    # Through the client and the stub server, so the wire strings are what is parsed.
    with Comfy() as client:
        job = client.jobs.get(client.submit(_wf(client)).id)

    assert job.created_at == datetime(2026, 7, 10, 18, 20, tzinfo=timezone.utc)
    assert job.expires_at == datetime(2026, 7, 11, 18, 20, tzinfo=timezone.utc)
    assert job.started_at is None
    assert job.completed_at is None
    assert job.queue_position == 0
    assert job.progress is None
    assert job.metrics == {"queue_ms": 9000, "execution_ms": None}
    assert job.urls.self == f"/api/v2/jobs/{job.id}"


async def test_async_fetched_job_exposes_its_state(server) -> None:
    async with AsyncComfy() as client:
        job = await client.jobs.get((await client.submit(_wf(client))).id)

    assert job.created_at == datetime(2026, 7, 10, 18, 20, tzinfo=timezone.utc)
    assert job.expires_at == datetime(2026, 7, 11, 18, 20, tzinfo=timezone.utc)
    assert job.started_at is None
    assert job.completed_at is None
    assert job.queue_position == 0
    assert job.progress is None
    assert job.metrics == {"queue_ms": 9000, "execution_ms": None}
    assert job.urls.self == f"/api/v2/jobs/{job.id}"
