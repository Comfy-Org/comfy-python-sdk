"""A failed job's category, read as a typed value on every path that carries it.

The stub server in conftest.py reports ``server.state.job_failure`` as the
``error`` of a job whose status is ``failed``, on the poll, the submit snapshot
and the cancel snapshot. Its stream status frame never carries an error, which
is the contract's ``StatusEvent`` shape.
"""

from __future__ import annotations

import pytest

from comfy_sdk import AsyncComfy, Comfy, FailureCategory, JobFailed, StatusChange

WIRE_VALUES = [
    "invalid_input",
    "content_refused",
    "out_of_gpu_memory",
    "missing_model_or_node",
    "workflow_logic",
    "other",
]


def _error(category: str | None = "missing_model_or_node") -> dict:
    error = {"code": "node_execution_error", "message": "node 7 failed"}
    if category is not None:
        error["category"] = category
    return error


def _fail(server, error: dict | None) -> None:
    server.state.terminal_status = "failed"
    server.state.job_failure = error


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_the_six_wire_values_are_the_enum_values_in_order() -> None:
    assert [c.value for c in FailureCategory] == WIRE_VALUES


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("invalid_input", FailureCategory.INVALID_INPUT),
        ("content_refused", FailureCategory.CONTENT_REFUSED),
        ("out_of_gpu_memory", FailureCategory.OUT_OF_GPU_MEMORY),
        ("missing_model_or_node", FailureCategory.MISSING_MODEL_OR_NODE),
        ("workflow_logic", FailureCategory.WORKFLOW_LOGIC),
        ("other", FailureCategory.OTHER),
    ],
)
def test_a_known_category_reads_as_its_member_on_the_handle_and_the_exception(
    server, wire: str, expected: FailureCategory
) -> None:
    _fail(server, _error(wire))
    with Comfy() as client:
        job = client.jobs.get("job_01")
        assert job.failure_category is expected
        with pytest.raises(JobFailed) as caught:
            job.result()
    assert caught.value.failure_category is expected


@pytest.mark.parametrize(
    "error",
    [_error(None), None],
    ids=["error without a category", "no error at all"],
)
def test_a_failed_job_without_a_category_reads_as_none(server, error: dict | None) -> None:
    _fail(server, error)
    with Comfy() as client:
        job = client.jobs.get("job_01")
        assert job.failure_category is None
        with pytest.raises(JobFailed) as caught:
            job.result()
    assert caught.value.failure_category is None


def test_a_succeeded_job_has_no_category(server) -> None:
    with Comfy() as client:
        assert client.run(_wf(client)).failure_category is None


def test_an_unknown_category_reads_as_other_and_the_raw_error_keeps_the_string(server) -> None:
    _fail(server, _error("quota_exceeded"))
    with Comfy() as client:
        job = client.jobs.get("job_01")
        assert job.failure_category is FailureCategory.OTHER
        with pytest.raises(JobFailed) as caught:
            job.result()
    assert caught.value.failure_category is FailureCategory.OTHER
    assert job.error.category == "quota_exceeded"
    assert caught.value.error.category == "quota_exceeded"


def test_a_job_failed_exception_built_by_hand_without_an_error_has_no_category() -> None:
    assert JobFailed("job ended failed").failure_category is None


def test_submit_cancel_get_and_stream_agree_on_the_error_and_category(server) -> None:
    _fail(server, _error("invalid_input"))
    server.state.submit_status = "failed"
    server.state.cancel_status = "failed"
    with Comfy() as client:
        submitted = client.submit(_wf(client))
        cancelled = submitted.cancel()
        fetched = client.jobs.get(submitted.id)
        streamed = client.jobs.get(submitted.id)
        terminal = list(streamed.events())[-1]

    assert submitted.failure_category is FailureCategory.INVALID_INPUT
    assert cancelled.failure_category is FailureCategory.INVALID_INPUT
    assert fetched.failure_category is FailureCategory.INVALID_INPUT
    assert isinstance(terminal, StatusChange)
    assert terminal.status == "failed"
    assert terminal.error == fetched.error
    assert streamed.failure_category is FailureCategory.INVALID_INPUT


async def test_async_submit_cancel_get_and_stream_agree_on_the_error_and_category(
    server,
) -> None:
    _fail(server, _error("invalid_input"))
    server.state.submit_status = "failed"
    server.state.cancel_status = "failed"
    async with AsyncComfy() as client:
        submitted = await client.submit(_wf(client))
        cancelled = await submitted.cancel()
        fetched = await client.jobs.get(submitted.id)
        streamed = await client.jobs.get(submitted.id)
        terminal = [e async for e in streamed.events()][-1]

    assert submitted.failure_category is FailureCategory.INVALID_INPUT
    assert cancelled.failure_category is FailureCategory.INVALID_INPUT
    assert fetched.failure_category is FailureCategory.INVALID_INPUT
    assert isinstance(terminal, StatusChange)
    assert terminal.status == "failed"
    assert terminal.error == fetched.error
    assert streamed.failure_category is FailureCategory.INVALID_INPUT


def test_a_failed_terminal_status_reads_the_job_once_and_updates_the_handle(server) -> None:
    _fail(server, _error("out_of_gpu_memory"))
    with Comfy() as client:
        job = client.submit(_wf(client))
        assert job.failure_category is None  # still queued: nothing failed yet
        seen = list(job.events())

    assert server.state.job_poll_count == 1
    assert seen[-1].status == "failed"
    assert seen[-1].error.category == "out_of_gpu_memory"
    assert job.status == "failed"
    assert job.failure_category is FailureCategory.OUT_OF_GPU_MEMORY


def test_a_canceled_terminal_status_reads_the_job_once(server) -> None:
    server.state.terminal_status = "canceled"
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    assert server.state.job_poll_count == 1
    assert seen[-1].status == "canceled"
    assert seen[-1].error is None
    assert job.status == "canceled"


def test_a_succeeded_terminal_status_reads_nothing_more(server) -> None:
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    assert server.state.job_poll_count == 0
    assert seen[-1].status == "succeeded"
    assert seen[-1].error is None


def test_a_failed_reread_still_yields_the_terminal_status_without_an_error(server) -> None:
    _fail(server, _error("invalid_input"))
    server.state.job_not_found = True
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    assert seen[-1] == StatusChange(status="failed")
    assert job.failure_category is None


def test_a_stream_that_ends_early_polls_to_the_terminal_status_with_the_error(server) -> None:
    _fail(server, _error("workflow_logic"))
    server.state.sse_mode = "reconnect"  # one progress frame, then a clean close
    with Comfy() as client:
        job = client.submit(_wf(client))
        seen = list(job.events())

    assert server.state.events_connect_count == 1
    assert server.state.job_poll_count == 1
    assert seen[-1].status == "failed"
    assert seen[-1].error.category == "workflow_logic"
    assert job.failure_category is FailureCategory.WORKFLOW_LOGIC


async def test_async_failed_terminal_status_reads_the_job_once_and_updates_the_handle(
    server,
) -> None:
    _fail(server, _error("content_refused"))
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [e async for e in job.events()]

    assert server.state.job_poll_count == 1
    assert seen[-1].status == "failed"
    assert seen[-1].error.category == "content_refused"
    assert job.failure_category is FailureCategory.CONTENT_REFUSED


async def test_async_succeeded_terminal_status_reads_nothing_more(server) -> None:
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [e async for e in job.events()]

    assert server.state.job_poll_count == 0
    assert seen[-1].error is None


async def test_async_failed_reread_still_yields_the_terminal_status_without_an_error(
    server,
) -> None:
    _fail(server, _error("invalid_input"))
    server.state.job_not_found = True
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [e async for e in job.events()]

    assert seen[-1] == StatusChange(status="failed")


async def test_async_stream_that_ends_early_polls_to_the_terminal_status_with_the_error(
    server,
) -> None:
    _fail(server, _error("workflow_logic"))
    server.state.sse_mode = "reconnect"
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [e async for e in job.events()]

    assert seen[-1].status == "failed"
    assert seen[-1].error.category == "workflow_logic"
