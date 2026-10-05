"""Job metadata: labels sent on submit, read back on the job, and filtered by ``list_jobs``.

The server owns every rule about the labels (how many, which key characters,
how long a value), so these tests pin only what the SDK does: what it sends,
what it reads back, how it pages, and that the server's refusal reaches the
caller with the key it named. Everything runs against the stub in
``conftest.py``.
"""

from __future__ import annotations

import pytest

from comfy_sdk import AsyncComfy, Comfy, ComfyError, JobSummary

_GRAPH = {"3": {"class_type": "KSampler", "inputs": {}}}
_LABELS = {"client": "acme", "run": "nightly-42"}


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json(_GRAPH)


def _item(job_id: str, metadata: dict[str, str] | None = None) -> dict:
    item = {
        "id": job_id,
        "status": "completed",
        "create_time": "2026-10-05T12:00:00Z",
        "update_time": "2026-10-05T12:01:00Z",
        "deployment_id": "dep_01",
    }
    if metadata is not None:
        item["metadata"] = metadata
    return item


# --- submit ---------------------------------------------------------------


def test_submit_sends_metadata_beside_the_workflow(server) -> None:
    with Comfy() as client:
        job = client.submit(_wf(client), metadata=_LABELS)
    body = server.state.last_jobs_body
    assert body is not None
    assert body["metadata"] == _LABELS
    assert body["workflow"] == _GRAPH
    assert job.metadata == _LABELS


async def test_async_submit_sends_metadata_beside_the_workflow(server) -> None:
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client), metadata=_LABELS)
    body = server.state.last_jobs_body
    assert body is not None
    assert body["metadata"] == _LABELS
    assert job.metadata == _LABELS


@pytest.mark.parametrize("metadata", [None, {}])
def test_submit_without_metadata_sends_the_body_it_always_did(server, metadata) -> None:
    with Comfy() as client:
        job = client.submit(_wf(client), metadata=metadata)
    assert server.state.last_jobs_body == {"workflow": _GRAPH}
    assert job.metadata == {}


async def test_async_submit_without_metadata_sends_the_body_it_always_did(server) -> None:
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
    assert server.state.last_jobs_body == {"workflow": _GRAPH}
    assert job.metadata == {}


def test_a_refused_map_raises_the_servers_error_naming_the_key(server) -> None:
    server.state.job_error = (422, "metadata_invalid")
    server.state.job_error_message = (
        'metadata key "bad key" has a character outside A-Z a-z 0-9 _ - .'
    )
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.submit(_wf(client), metadata={"bad key": "x"})
    assert excinfo.value.code == "metadata_invalid"
    assert excinfo.value.http_status == 422
    assert "bad key" in str(excinfo.value)
    # A 422 is final: the submit is not retried.
    assert server.state.submit_count == 1


async def test_async_refused_map_raises_the_servers_error_naming_the_key(server) -> None:
    server.state.job_error = (422, "metadata_invalid")
    server.state.job_error_message = 'metadata key "k16" is one pair too many (17 > 16)'
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.submit(_wf(client), metadata={f"k{i}": "v" for i in range(17)})
    assert excinfo.value.code == "metadata_invalid"
    assert excinfo.value.http_status == 422
    assert "k16" in str(excinfo.value)


# --- reading a job --------------------------------------------------------


def test_a_fetched_job_exposes_its_metadata(server) -> None:
    server.state.job_metadata = _LABELS
    with Comfy() as client:
        job = client.jobs.get("job_01")
        assert job.metadata == _LABELS
        # A copy: editing it does not change the handle.
        job.metadata["client"] = "other"
        assert job.metadata == _LABELS


def test_a_job_without_metadata_exposes_an_empty_dict(server) -> None:
    with Comfy() as client:
        job = client.jobs.get("job_01")
    assert job.metadata == {}


async def test_an_async_fetched_job_exposes_its_metadata(server) -> None:
    server.state.job_metadata = _LABELS
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_01")
        assert job.metadata == _LABELS
        await job.refresh()
        assert job.metadata == _LABELS


# --- list_jobs ------------------------------------------------------------


def test_list_jobs_sends_filters_and_follows_the_cursor_to_the_last_page(server) -> None:
    server.state.job_list_pages = [
        [_item("job_05", _LABELS), _item("job_04", _LABELS)],
        [_item("job_03", _LABELS), _item("job_02", _LABELS)],
        [_item("job_01", _LABELS)],
    ]
    with Comfy() as client:
        found = list(client.list_jobs(metadata={"client": "acme", "run": "nightly-42"}, limit=2))
    assert [j.id for j in found] == ["job_05", "job_04", "job_03", "job_02", "job_01"]
    assert all(j.metadata == _LABELS for j in found)
    queries = server.state.job_list_queries
    # One request per page, and none after the page with no `next_cursor`.
    assert len(queries) == 3
    for q in queries:
        assert q["metadata[client]"] == ["acme"]
        assert q["metadata[run]"] == ["nightly-42"]
        assert q["limit"] == ["2"]
    assert "cursor" not in queries[0]
    assert queries[1]["cursor"] == ["page-1"]
    assert queries[2]["cursor"] == ["page-2"]


async def test_async_list_jobs_sends_filters_and_follows_the_cursor(server) -> None:
    server.state.job_list_pages = [[_item("job_02")], [_item("job_01")]]
    async with AsyncComfy() as client:
        found = [j async for j in client.list_jobs(metadata={"client": "acme"}, limit=1)]
    assert [j.id for j in found] == ["job_02", "job_01"]
    queries = server.state.job_list_queries
    assert len(queries) == 2
    assert queries[0]["metadata[client]"] == ["acme"]
    assert queries[1]["metadata[client]"] == ["acme"]
    assert queries[1]["cursor"] == ["page-1"]


def test_list_jobs_with_no_arguments_sends_no_query(server) -> None:
    server.state.job_list_pages = [[_item("job_01")]]
    with Comfy() as client:
        found = list(client.list_jobs())
    assert [j.id for j in found] == ["job_01"]
    assert server.state.job_list_queries == [{}]


def test_list_jobs_items_keep_the_server_fields(server) -> None:
    item = _item("job_01")
    server.state.job_list_pages = [[item]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    assert isinstance(summary, JobSummary)
    assert summary.id == "job_01"
    assert summary.status == "completed"
    assert summary.metadata == {}
    assert summary.data == item


def test_list_jobs_surfaces_a_refused_filter(server) -> None:
    server.state.job_list_error = (400, "invalid_metadata_filter", "at most 3 metadata filters")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            list(client.list_jobs(metadata={"a": "1", "b": "2", "c": "3", "d": "4"}))
    assert excinfo.value.code == "invalid_metadata_filter"
    assert excinfo.value.http_status == 400


async def test_async_list_jobs_surfaces_a_refused_filter(server) -> None:
    server.state.job_list_error = (400, "invalid_metadata_filter", "at most 3 metadata filters")
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            [j async for j in client.list_jobs(metadata={"a": "1", "b": "2", "c": "3", "d": "4"})]
    assert excinfo.value.code == "invalid_metadata_filter"
