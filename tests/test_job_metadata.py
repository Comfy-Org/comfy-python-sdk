"""Job metadata: labels sent on submit, read back on the job, and filtered by ``list_jobs``.

The server owns every rule about the labels (how many, which key characters,
how long a value), so these tests pin only what the SDK does: what it sends,
what it reads back, how it pages, and that the server's refusal reaches the
caller with the server's own message: the key it named, or the count of pairs.
Everything runs against the stub in ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pickle
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from conftest import _job_json

import comfy_sdk.client as _client_module
from comfy_low.models import Job as LowJob
from comfy_low.models import JobStatus
from comfy_sdk import AsyncComfy, Comfy, ComfyError, InvalidWorkflow, JobSummary
from comfy_sdk.router_exceptions import RateLimited

_GRAPH = {"3": {"class_type": "KSampler", "inputs": {}}}
_LABELS = {"client": "acme", "run": "nightly-42"}


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json(_GRAPH)


def _item(job_id: str, metadata: dict[str, str] | None = None) -> dict:
    item = {
        "id": job_id,
        "status": "succeeded",
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
    # The gateway's message for a bad key: it names the key.
    server.state.job_error_message = (
        'metadata key "bad key" must be 1 to 40 characters from A-Z a-z 0-9 _ - .'
    )
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.submit(_wf(client), metadata={"bad key": "x"})
    assert excinfo.value.code == "metadata_invalid"
    assert excinfo.value.http_status == 422
    # The base class, not the workflow error: the workflow itself was fine.
    assert not isinstance(excinfo.value, InvalidWorkflow)
    assert "bad key" in str(excinfo.value)
    # A 422 is final: the submit is not retried.
    assert server.state.submit_count == 1


async def test_async_too_many_pairs_raises_the_servers_error_with_the_count(server) -> None:
    # The gateway's message for too many pairs: the count and the limit, no key.
    message = "metadata has 17 pairs; at most 16 are allowed"
    server.state.job_error = (422, "metadata_invalid")
    server.state.job_error_message = message
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.submit(_wf(client), metadata={f"k{i}": "v" for i in range(17)})
    assert excinfo.value.code == "metadata_invalid"
    assert excinfo.value.http_status == 422
    # The base class, not the workflow error: the workflow itself was fine.
    assert not isinstance(excinfo.value, InvalidWorkflow)
    assert message in str(excinfo.value)
    # A 422 is final: the submit is not retried.
    assert server.state.submit_count == 1


def test_a_host_without_label_support_refuses_with_its_own_code(server) -> None:
    server.state.job_error = (422, "metadata_not_supported")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            client.submit(_wf(client), metadata=_LABELS)
    assert excinfo.value.code == "metadata_not_supported"
    assert excinfo.value.http_status == 422
    assert not isinstance(excinfo.value, InvalidWorkflow)
    assert server.state.submit_count == 1


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


def test_a_cancelled_job_keeps_its_metadata(server) -> None:
    server.state.job_metadata = _LABELS
    with Comfy() as client:
        job = client.jobs.get("job_01")
        assert job.cancel().metadata == _LABELS


async def test_an_async_cancelled_job_keeps_its_metadata(server) -> None:
    server.state.job_metadata = _LABELS
    async with AsyncComfy() as client:
        job = await client.jobs.get("job_01")
        assert (await job.cancel()).metadata == _LABELS


# --- metadata that is not a map of strings ----------------------------------

# What a server might send that is not a map of strings. The self-hosted proxy
# has its own `metadata`, a plain string; and nothing on the wire stops a map
# value being a number or null. None of these may raise: on submit the job
# already exists by the time the answer is read.
_ODD_METADATA = [
    pytest.param({"n": 1}, {}, id="number-value"),
    pytest.param({"n": None, "client": "acme"}, {"client": "acme"}, id="null-value"),
    pytest.param("batch-7", {}, id="string"),
    pytest.param(["batch-7"], {}, id="list"),
    pytest.param({"__proto__": "x"}, {"__proto__": "x"}, id="proto-key"),
]


@pytest.mark.parametrize(("sent", "read"), _ODD_METADATA)
def test_odd_metadata_on_a_job_reads_as_labels_and_never_raises(server, sent, read) -> None:
    server.state.job_metadata = sent
    with Comfy() as client:
        job = client.submit(_wf(client))
        assert job.metadata == read
        assert client.jobs.get(job.id).metadata == read
        assert job.refresh().metadata == read
        assert job.wait().metadata == read
        assert job.cancel().metadata == read


@pytest.mark.parametrize(("sent", "read"), _ODD_METADATA)
async def test_async_odd_metadata_on_a_job_reads_as_labels_and_never_raises(
    server, sent, read
) -> None:
    server.state.job_metadata = sent
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        assert job.metadata == read
        assert (await client.jobs.get(job.id)).metadata == read
        assert (await job.refresh()).metadata == read
        assert (await job.wait()).metadata == read
        assert (await job.cancel()).metadata == read


@pytest.mark.parametrize(("sent", "read"), _ODD_METADATA)
def test_odd_metadata_on_a_list_item_reads_as_labels_and_never_raises(server, sent, read) -> None:
    item: dict[str, Any] = {**_item("job_01"), "metadata": sent}
    server.state.job_list_pages = [[item]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    assert summary.metadata == read
    assert summary.data["metadata"] == sent


# Keys the lenient read must keep exactly as sent, whatever they look like.
_ODD_KEYS = {
    "__proto__": "a",
    "constructor": "b",
    "": "empty key",
    "a.b-c_d": "punctuation",
    "ключ": "значение",
}


def test_every_string_label_survives_whatever_its_key(server) -> None:
    server.state.job_metadata = _ODD_KEYS
    server.state.job_list_pages = [[_item("job_01", _ODD_KEYS)]]
    with Comfy() as client:
        assert client.submit(_wf(client)).metadata == _ODD_KEYS
        assert client.jobs.get("job_01").metadata == _ODD_KEYS
        (summary,) = client.list_jobs()
    assert summary.metadata == _ODD_KEYS


def test_a_job_with_metadata_pickles(server) -> None:
    server.state.job_metadata = _LABELS
    with Comfy() as client:
        low = client._low
        for model in (
            low.post_jobs(_GRAPH),
            low.get_job("job_01"),
            low.cancel_job("job_01"),
        ):
            copy = pickle.loads(pickle.dumps(model))
            assert copy == model
            assert copy.metadata == _LABELS


async def test_an_async_job_with_metadata_pickles(server) -> None:
    server.state.job_metadata = _LABELS
    async with AsyncComfy() as client:
        low = client._low
        for model in (
            await low.post_jobs(_GRAPH),
            await low.get_job("job_01"),
            await low.cancel_job("job_01"),
        ):
            copy = pickle.loads(pickle.dumps(model))
            assert copy == model
            assert copy.metadata == _LABELS


def test_odd_metadata_still_reads_once_the_generated_job_declares_a_strict_field() -> None:
    # A spec sync will add `metadata: dict[str, str]` to the generated `Job`.
    # Simulate that model and check the transport's lenient reading still wins.
    from comfy_low.transport import _with_lenient_metadata

    class SyncedJob(LowJob):
        metadata: dict[str, str] | None = None

    body = _job_json("job_01", "queued", metadata={"n": 1, "client": "acme"})
    with pytest.raises(ValueError):
        SyncedJob.model_validate(body)
    model = _with_lenient_metadata(SyncedJob).model_validate(body)
    assert getattr(model, "metadata", None) == {"client": "acme"}


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
    server.state.job_list_pages = [[_item("job_02", _LABELS)], [_item("job_01", _LABELS)]]
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
    # The bare path, not `/jobs?`.
    assert server.state.job_list_paths == ["/api/v2/jobs"]


def test_list_jobs_items_keep_the_server_fields(server) -> None:
    item = _item("job_01")
    server.state.job_list_pages = [[item]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    assert isinstance(summary, JobSummary)
    assert summary.id == "job_01"
    # A JobStatus value, the same string `Job.status` reports for that state.
    assert summary.status == "succeeded"
    assert JobStatus(summary.status) is JobStatus.succeeded
    assert summary.create_time == datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert summary.update_time == datetime(2026, 10, 5, 12, 1, tzinfo=timezone.utc)
    assert summary.deployment_id == "dep_01"
    assert summary.metadata == {}
    assert summary.data == item


def test_a_filtered_list_skips_items_whose_labels_do_not_match(server) -> None:
    # A host that ignores the filters (a gateway without label support) sends
    # its jobs unfiltered, page after page; the SDK keeps only the matches.
    server.state.job_list_pages = [
        [
            _item("job_06", _LABELS),
            _item("job_05", {"client": "other", "run": "nightly-42"}),
            _item("job_04", {"client": "acme"}),
            _item("job_03"),
        ],
        [{**_item("job_02"), "metadata": "client=acme"}, _item("job_01", _LABELS)],
    ]
    with Comfy() as client:
        found = list(client.list_jobs(metadata={"client": "acme", "run": "nightly-42"}))
    assert [j.id for j in found] == ["job_06", "job_01"]
    assert len(server.state.job_list_queries) == 2


def test_a_filtered_list_compares_values_as_the_text_the_query_sends(server) -> None:
    # `metadata={"run": 7}` is sent as `run=7`, which the server matches against
    # the label "7"; the client-side check must compare the same text.
    server.state.job_list_pages = [[_item("job_02", {"run": "7"}), _item("job_01", {"run": "8"})]]
    with Comfy() as client:
        found = list(client.list_jobs(metadata={"run": 7}))  # type: ignore[dict-item]
    assert [j.id for j in found] == ["job_02"]
    assert server.state.job_list_queries[0]["metadata[run]"] == ["7"]


@pytest.mark.parametrize(
    ("filters", "sent"),
    [
        pytest.param({1: "a"}, ("metadata[1]", "a"), id="int-key"),
        pytest.param({"1": b"a"}, ("metadata[1]", "a"), id="bytes-value"),
    ],
)
def test_a_filtered_list_compares_exactly_the_pairs_the_query_sends(server, filters, sent) -> None:
    # The check must compare the key and the value as sent, not as given.
    server.state.job_list_pages = [[_item("job_02", {"1": "a"}), _item("job_01", {"1": "b"})]]
    with Comfy() as client:
        found = list(client.list_jobs(metadata=filters))
    assert [j.id for j in found] == ["job_02"]
    key, value = sent
    assert server.state.job_list_queries[0][key] == [value]


async def test_async_filtered_list_skips_items_whose_labels_do_not_match(server) -> None:
    server.state.job_list_pages = [[_item("job_02"), _item("job_01", _LABELS)]]
    async with AsyncComfy() as client:
        found = [j async for j in client.list_jobs(metadata={"client": "acme"})]
    assert [j.id for j in found] == ["job_01"]


def test_a_filtered_list_on_a_host_without_labels_yields_nothing(server) -> None:
    # The self-hosted proxy's list: one page of its newest jobs, no filtering,
    # no `next_cursor`, and its `metadata` a string.
    server.state.job_list_pages = [[{**_item("job_01"), "metadata": "batch-7"}]]
    with Comfy() as client:
        assert list(client.list_jobs(metadata={"client": "acme"})) == []


def test_a_list_page_still_rate_limited_after_its_budget_raises(server, monkeypatch) -> None:
    monkeypatch.setattr(_client_module, "_QUEUE_RETRY_BUDGET", 5.0)
    clock = [100.0, 101.0, 106.0]  # the page's deadline is 105

    def _now() -> float:
        return clock.pop(0) if len(clock) > 1 else clock[0]

    monkeypatch.setattr(_client_module, "_now", _now)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    server.state.job_list_pages = [[_item("job_01")]]
    # Ten 429s, then the page: only a deadline stops the retries before it.
    server.state.job_list_429_at = set(range(10))
    server.state.job_list_retry_after = "3"
    with Comfy() as client:
        with pytest.raises(RateLimited):
            list(client.list_jobs())
    assert sleeps == [3]
    assert len(server.state.job_list_queries) == 2


def test_a_list_page_429_without_retry_after_raises_at_once(server, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    server.state.job_list_pages = [[_item("job_01")]]
    server.state.job_list_429_at = {0}
    server.state.job_list_retry_after = None
    with Comfy() as client:
        with pytest.raises(RateLimited):
            list(client.list_jobs())
    assert sleeps == []
    assert len(server.state.job_list_queries) == 1


def test_list_jobs_waits_at_least_a_second_on_retry_after_zero(server, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    server.state.job_list_pages = [[_item("job_01")]]
    server.state.job_list_429_at = {0}
    server.state.job_list_retry_after = "0"
    with Comfy() as client:
        assert [j.id for j in client.list_jobs()] == ["job_01"]
    assert sleeps == [1.0]


async def test_async_list_jobs_waits_at_least_a_second_on_retry_after_zero(
    server, monkeypatch
) -> None:
    sleeps: list[float] = []

    async def _no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    server.state.job_list_pages = [[_item("job_01")]]
    server.state.job_list_429_at = {0}
    server.state.job_list_retry_after = "0"
    async with AsyncComfy() as client:
        assert [j.id async for j in client.list_jobs()] == ["job_01"]
    assert sleeps == [1.0]


def test_a_job_summary_can_be_hashed(server) -> None:
    server.state.job_list_pages = [[_item("job_01", _LABELS)]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    assert summary in {summary}


def test_a_job_summary_prints_without_the_raw_item(server) -> None:
    # The raw item can hold the workflow and node logs; printing a summary must
    # not write them into the caller's logs, as printing a `Job` does not.
    item = {**_item("job_01", _LABELS), "workflow": {"secret_node": {"inputs": {}}}}
    server.state.job_list_pages = [[item]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    printed = repr(summary)
    assert "job_01" in printed
    assert "secret_node" not in printed
    assert ", data=" not in printed
    # Still part of equality: two summaries of different items differ.
    assert summary != dataclasses.replace(summary, data={**item, "extra": 1})
    assert hash(summary) == hash(dataclasses.replace(summary, data={}))


def test_a_page_without_jobs_yields_nothing_and_paging_continues(server) -> None:
    server.state.job_list_pages = [None, [_item("job_01")]]
    with Comfy() as client:
        found = list(client.list_jobs())
    assert [j.id for j in found] == ["job_01"]


def test_a_last_page_without_jobs_yields_nothing(server) -> None:
    server.state.job_list_pages = [None]
    with Comfy() as client:
        assert list(client.list_jobs()) == []


async def test_async_a_page_without_jobs_yields_nothing_and_paging_continues(server) -> None:
    server.state.job_list_pages = [None, [_item("job_01")]]
    async with AsyncComfy() as client:
        found = [j async for j in client.list_jobs()]
    assert [j.id for j in found] == ["job_01"]


def test_list_jobs_retries_a_429_on_each_page_at_the_servers_pace(server, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    server.state.job_list_pages = [[_item("job_02", _LABELS)], [_item("job_01", _LABELS)]]
    # The first request for each page is answered 429 `Retry-After: 3`.
    server.state.job_list_429_at = {0, 2}
    server.state.job_list_retry_after = "3"
    with Comfy() as client:
        found = list(client.list_jobs(metadata={"client": "acme"}, limit=1))
    assert [j.id for j in found] == ["job_02", "job_01"]
    assert sleeps == [3, 3]
    queries = server.state.job_list_queries
    assert len(queries) == 4
    # The retry asks for the same page with the same filters.
    assert queries[1] == queries[0]
    assert queries[3] == queries[2]
    assert queries[3]["cursor"] == ["page-1"]


async def test_async_list_jobs_retries_a_429_on_each_page(server, monkeypatch) -> None:
    sleeps: list[float] = []

    async def _no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    server.state.job_list_pages = [[_item("job_02")], [_item("job_01")]]
    server.state.job_list_429_at = {0, 2}
    server.state.job_list_retry_after = "3"
    async with AsyncComfy() as client:
        found = [j async for j in client.list_jobs(limit=1)]
    assert [j.id for j in found] == ["job_02", "job_01"]
    assert sleeps == [3, 3]
    assert len(server.state.job_list_queries) == 4


def test_list_jobs_items_without_optional_fields_read_as_none(server) -> None:
    server.state.job_list_pages = [[{"id": "job_01", "status": "queued"}]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    assert summary.create_time is None
    assert summary.update_time is None
    assert summary.deployment_id is None
    assert summary.metadata == {}


def _without(field: str) -> dict:
    item = _item("job_01")
    del item[field]
    return item


def _with_null(field: str) -> dict:
    item = _item("job_01")
    item[field] = None
    return item


# Each case: the list item the server sends, and the text the error must name.
_BAD_LIST_ITEMS = [
    pytest.param(_without("id"), "id", id="id-missing"),
    pytest.param(_without("status"), "status", id="status-missing"),
    pytest.param(_with_null("id"), "id", id="id-null"),
    pytest.param(_with_null("status"), "status", id="status-null"),
    pytest.param("job_01", "not a JSON object", id="not-an-object"),
]


@pytest.mark.parametrize(("item", "named"), _BAD_LIST_ITEMS)
def test_a_bad_list_item_raises_invalid_response(server, item, named) -> None:
    # Raised, not skipped: skipping would hide a job from the caller.
    server.state.job_list_pages = [[item]]
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            list(client.list_jobs())
    assert excinfo.value.code == "invalid_response"
    assert named in str(excinfo.value)


@pytest.mark.parametrize(("item", "named"), _BAD_LIST_ITEMS)
async def test_async_bad_list_item_raises_invalid_response(server, item, named) -> None:
    server.state.job_list_pages = [[item]]
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            [j async for j in client.list_jobs()]
    assert excinfo.value.code == "invalid_response"
    assert named in str(excinfo.value)


# A page whose `jobs` is not an array: an object would otherwise iterate its keys.
_BAD_JOBS_FIELDS = [
    pytest.param(42, id="integer"),
    pytest.param("job_01", id="string"),
    pytest.param({"id": "job_01", "status": "queued"}, id="object"),
]


@pytest.mark.parametrize("jobs", _BAD_JOBS_FIELDS)
def test_a_page_whose_jobs_is_not_an_array_raises_invalid_response(server, jobs) -> None:
    server.state.job_list_pages = [jobs]
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            list(client.list_jobs())
    assert excinfo.value.code == "invalid_response"
    assert "'jobs' is not an array" in str(excinfo.value)


@pytest.mark.parametrize("jobs", _BAD_JOBS_FIELDS)
async def test_async_a_page_whose_jobs_is_not_an_array_raises_invalid_response(
    server, jobs
) -> None:
    server.state.job_list_pages = [jobs]
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            [j async for j in client.list_jobs()]
    assert excinfo.value.code == "invalid_response"
    assert "'jobs' is not an array" in str(excinfo.value)


def test_list_jobs_reads_nanosecond_times_and_tolerates_unreadable_ones(server) -> None:
    item = {
        "id": "job_01",
        "status": "succeeded",
        "create_time": "2026-10-05T12:00:00.123456789Z",
        "update_time": "not a time",
    }
    server.state.job_list_pages = [[item]]
    with Comfy() as client:
        (summary,) = client.list_jobs()
    assert summary.create_time is not None
    assert summary.create_time.tzinfo is not None
    assert summary.create_time.replace(microsecond=0) == datetime(
        2026, 10, 5, 12, 0, tzinfo=timezone.utc
    )
    assert summary.update_time is None
    assert summary.data["update_time"] == "not a time"


def test_list_jobs_surfaces_a_refused_filter(server) -> None:
    server.state.job_list_error = (400, "invalid_metadata_filter", "at most 3 metadata filters")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            list(client.list_jobs(metadata={"a": "1", "b": "2", "c": "3", "d": "4"}))
    assert excinfo.value.code == "invalid_metadata_filter"
    assert excinfo.value.http_status == 400


def test_list_jobs_surfaces_a_refused_cursor(server) -> None:
    server.state.job_list_error = (400, "invalid_cursor", "cursor was not issued by this list")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            list(client.list_jobs())
    assert excinfo.value.code == "invalid_cursor"
    assert excinfo.value.http_status == 400
    assert not isinstance(excinfo.value, InvalidWorkflow)


def test_list_jobs_on_a_host_that_cannot_list_raises_not_implemented(server) -> None:
    # Comfy Cloud's answer until it lists jobs.
    server.state.job_list_error = (501, "not_implemented", "listing jobs is not supported here")
    with Comfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            list(client.list_jobs())
    assert excinfo.value.code == "not_implemented"
    assert excinfo.value.http_status == 501
    # A 501 is final: the page is not retried.
    assert len(server.state.job_list_queries) == 1


async def test_async_list_jobs_surfaces_a_refused_filter(server) -> None:
    server.state.job_list_error = (400, "invalid_metadata_filter", "at most 3 metadata filters")
    async with AsyncComfy() as client:
        with pytest.raises(ComfyError) as excinfo:
            [j async for j in client.list_jobs(metadata={"a": "1", "b": "2", "c": "3", "d": "4"})]
    assert excinfo.value.code == "invalid_metadata_filter"
