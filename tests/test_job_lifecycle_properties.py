"""The lifecycle view on a job handle: timestamps, progress, queue position,
metrics and follow-up links.

Every one of these is a view onto the state the handle already holds — the
same contract the older ``id`` / ``status`` / ``outputs`` / ``error``
properties have. Reading one must not re-fetch, and the nullable wire fields
must reach the caller as ``None`` rather than as an exception or a raw string.

The populated and the empty shape both come from the stub server, driven
through ``server.state``, so what is under test is the whole path from the
response body to the property — including the timestamp parsing, which is the
part a caller cannot do for themselves if the SDK hands back a string.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from comfy_sdk import AsyncComfy, Comfy, Progress

_CREATED = datetime(2026, 7, 10, 18, 20, tzinfo=timezone.utc)
_EXPIRES = datetime(2026, 7, 11, 18, 20, tzinfo=timezone.utc)
_STARTED = datetime(2026, 7, 10, 18, 21, tzinfo=timezone.utc)
_COMPLETED = datetime(2026, 7, 10, 18, 23, 30, tzinfo=timezone.utc)

#: A full snapshot, every optional field of the schema included — this is what
#: proves nothing is dropped between the wire and ``job.progress``.
_PROGRESS_WIRE = {
    "value": 0.42,
    "nodes_done": 11,
    "nodes_total": 31,
    "current_node": "12",
    "current_node_class": "KSampler",
    "step": 21,
    "steps": 50,
    "message": "KSampler 21/50",
}
_PROGRESS = Progress(
    value=0.42,
    message="KSampler 21/50",
    nodes_done=11,
    nodes_total=31,
    current_node="12",
    step=21,
    steps=50,
    current_node_class="KSampler",
)
_METRICS = {"queue_ms": 9000, "execution_ms": 42000}


def _populated(state) -> None:
    """A job the server reports as started, finished, and mid-snapshot."""
    state.job_started_at = "2026-07-10T18:21:00Z"
    state.job_completed_at = "2026-07-10T18:23:30Z"
    state.job_progress = dict(_PROGRESS_WIRE)
    state.job_queue_position = 3
    state.job_metrics = dict(_METRICS)


def _empty(state) -> None:
    """The same job with every nullable field null — a queued job, and the
    shape a surface that reports no progress snapshot on a poll returns.
    """
    state.job_started_at = None
    state.job_completed_at = None
    state.job_progress = None
    state.job_queue_position = None
    state.job_metrics = None


# --- sync ----------------------------------------------------------------


def test_lifecycle_properties_populated(server) -> None:
    _populated(server.state)
    with Comfy() as client:
        job = client.jobs.get("job_abc")

        assert job.created_at == _CREATED
        assert job.started_at == _STARTED
        assert job.completed_at == _COMPLETED
        assert job.expires_at == _EXPIRES
        assert job.progress == _PROGRESS
        assert job.queue_position == 3
        assert job.metrics == _METRICS
        assert job.urls.self == "/api/v2/jobs/job_abc"
        assert job.urls.events == "/api/v2/jobs/job_abc/events"
        assert job.urls.cancel == "/api/v2/jobs/job_abc/cancel"


def test_duration_needs_no_private_attribute(server) -> None:
    # The reason the timestamps are parsed rather than passed through as
    # strings: subtracting them is the whole point, and it is the one thing a
    # caller cannot do without reaching into the model themselves.
    _populated(server.state)
    with Comfy() as client:
        job = client.jobs.get("job_abc")
        assert job.completed_at - job.started_at == timedelta(seconds=150)


def test_lifecycle_properties_null(server) -> None:
    _empty(server.state)
    with Comfy() as client:
        job = client.jobs.get("job_abc")

        assert job.started_at is None
        assert job.completed_at is None
        assert job.progress is None
        assert job.queue_position is None
        assert job.metrics is None
        # The three non-nullable fields of the contract have no null case:
        # a job always carries when it was created, when it expires, and the
        # links to follow.
        assert job.created_at == _CREATED
        assert job.expires_at == _EXPIRES
        assert job.urls.self == "/api/v2/jobs/job_abc"


def test_lifecycle_properties_do_not_refetch(server) -> None:
    # Same contract as the existing properties: a view onto handle state.
    # A property that polled would also turn any read into a network error.
    _populated(server.state)
    with Comfy() as client:
        job = client.jobs.get("job_abc")
        polls = server.state.job_poll_count
        server.state.job_not_found = True  # any re-fetch now raises

        assert job.started_at == _STARTED
        assert job.completed_at == _COMPLETED
        assert job.progress == _PROGRESS
        assert job.queue_position == 3
        assert job.metrics == _METRICS
        assert job.urls.self == "/api/v2/jobs/job_abc"
        assert server.state.job_poll_count == polls


def test_refresh_updates_the_lifecycle_view(server) -> None:
    # The flip side: the properties are not frozen at construction — a
    # refresh moves them, which is what makes the queued -> finished
    # transition observable at all.
    _empty(server.state)
    with Comfy() as client:
        job = client.jobs.get("job_abc")
        assert job.completed_at is None

        _populated(server.state)
        job.refresh()

        assert job.completed_at == _COMPLETED
        assert job.progress == _PROGRESS


# --- async ---------------------------------------------------------------


async def test_async_lifecycle_properties_populated(server) -> None:
    _populated(server.state)
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_abc")

        assert job.created_at == _CREATED
        assert job.started_at == _STARTED
        assert job.completed_at == _COMPLETED
        assert job.expires_at == _EXPIRES
        assert job.progress == _PROGRESS
        assert job.queue_position == 3
        assert job.metrics == _METRICS
        assert job.urls.self == "/api/v2/jobs/job_abc"
        assert job.urls.events == "/api/v2/jobs/job_abc/events"
        assert job.urls.cancel == "/api/v2/jobs/job_abc/cancel"


async def test_async_duration_needs_no_private_attribute(server) -> None:
    _populated(server.state)
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_abc")
        assert job.completed_at - job.started_at == timedelta(seconds=150)


async def test_async_lifecycle_properties_null(server) -> None:
    _empty(server.state)
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_abc")

        assert job.started_at is None
        assert job.completed_at is None
        assert job.progress is None
        assert job.queue_position is None
        assert job.metrics is None
        assert job.created_at == _CREATED
        assert job.expires_at == _EXPIRES
        assert job.urls.self == "/api/v2/jobs/job_abc"


async def test_async_lifecycle_properties_do_not_refetch(server) -> None:
    _populated(server.state)
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_abc")
        polls = server.state.job_poll_count
        server.state.job_not_found = True

        assert job.started_at == _STARTED
        assert job.completed_at == _COMPLETED
        assert job.progress == _PROGRESS
        assert job.queue_position == 3
        assert job.metrics == _METRICS
        assert job.urls.self == "/api/v2/jobs/job_abc"
        assert server.state.job_poll_count == polls


async def test_async_refresh_updates_the_lifecycle_view(server) -> None:
    _empty(server.state)
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_abc")
        assert job.completed_at is None

        _populated(server.state)
        await job.refresh()

        assert job.completed_at == _COMPLETED
        assert job.progress == _PROGRESS
