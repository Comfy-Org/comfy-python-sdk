"""On-demand execution logs: what a run printed, fetched only when asked.

The log is a resource of its own rather than a field on the job, so these
cover the two things that follow from that: a job read never carries it, and
"no log" is an ordinary answer rather than an error.
"""

from __future__ import annotations

import pytest

from comfy_sdk import AsyncComfy, Comfy, NotFound

_CAPTURED = {
    "text": "got prompt\nPrompt executed in 4.62 seconds\n",
    "truncated": False,
    "captured_at": "2026-07-10T18:25:00Z",
    "complete": True,
}


def _wf(client):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_get_logs_returns_what_the_run_printed(server) -> None:
    server.state.job_logs = _CAPTURED
    with Comfy() as client:
        job = client.run(_wf(client))
        logs = job.get_logs()

    assert logs is not None
    assert logs.text == "got prompt\nPrompt executed in 4.62 seconds\n"
    # Read off the wire, not hardcoded: this is the whole log, so `truncated`
    # is false here and true in the shed-entirely case below.
    assert logs.truncated is False
    assert logs.complete is True
    assert logs.captured_at.year == 2026


def test_running_a_job_never_fetches_the_log(server) -> None:
    # The whole point of the resource: submitting and polling to terminal must
    # not pay for a log the caller did not ask for.
    server.state.job_logs = _CAPTURED
    with Comfy() as client:
        client.run(_wf(client))

    assert server.state.job_logs_request_count == 0


def test_get_logs_is_none_when_the_job_has_no_log(server) -> None:
    server.state.job_logs = None
    with Comfy() as client:
        job = client.run(_wf(client))
        assert job.get_logs() is None
    assert server.state.job_logs_request_count == 1


def test_get_logs_is_none_without_a_link_and_makes_no_request(server) -> None:
    # A surface that serves no logs at all (Comfy Cloud, self-hosted) omits
    # urls.logs. The answer is known from its absence, so the SDK must not
    # construct the URL and ask anyway.
    server.state.omit_logs_link = True
    server.state.job_logs = _CAPTURED
    with Comfy() as client:
        job = client.run(_wf(client))
        assert job.get_logs() is None
    assert server.state.job_logs_request_count == 0


def test_get_logs_follows_the_link_rather_than_building_the_path(server) -> None:
    # The invariant the whole design rests on. The stub is mounted at the root,
    # so a synthesized `/jobs/{id}/logs` and the served link normally resolve to
    # the same URL and a regression would be silent; serving the link at a path
    # no synthesis could produce is what makes the difference observable. A
    # path-mounted proxy is the real case this protects.
    server.state.logs_link_path = "/api/v2/jobs/link-only-token/logs"
    server.state.job_logs = _CAPTURED
    with Comfy() as client:
        job = client.run(_wf(client))
        assert job.get_logs() is not None
    assert server.state.last_job_logs_path == "/api/v2/jobs/link-only-token/logs"


def test_get_logs_treats_an_empty_link_as_no_link(server) -> None:
    # A server that serializes the absent optional link as "" rather than
    # omitting the key must not send the SDK to `/jobs//logs`, which would
    # surface as a NotFound for a job that exists.
    server.state.empty_logs_link = True
    server.state.job_logs = _CAPTURED
    with Comfy() as client:
        job = client.run(_wf(client))
        assert job.get_logs() is None
    assert server.state.job_logs_request_count == 0


def test_get_logs_refetches_every_call(server) -> None:
    # Deliberately uncached: an early None on a job that had not finished must
    # not mask the log it goes on to produce.
    server.state.job_logs = None
    with Comfy() as client:
        job = client.run(_wf(client))
        assert job.get_logs() is None

        server.state.job_logs = _CAPTURED
        logs = job.get_logs()

    assert logs is not None
    assert logs.text.startswith("got prompt")
    assert server.state.job_logs_request_count == 2


def test_get_logs_raises_the_usual_error_for_a_missing_job(server) -> None:
    server.state.job_logs_not_found = True
    with Comfy() as client:
        job = client.run(_wf(client))
        with pytest.raises(NotFound):
            job.get_logs()


def test_a_captured_empty_log_is_not_absence(server) -> None:
    # truncated with an empty text is how a log shed entirely to fit says so,
    # which is a different answer from never having had one.
    server.state.job_logs = {
        "text": "",
        "truncated": True,
        "captured_at": "2026-07-10T18:25:00Z",
        "complete": True,
    }
    with Comfy() as client:
        job = client.run(_wf(client))
        logs = job.get_logs()

    assert logs is not None
    assert logs.text == ""
    assert logs.truncated is True


async def test_async_get_logs_mirrors_the_sync_surface(server) -> None:
    server.state.job_logs = _CAPTURED
    async with AsyncComfy() as client:
        wf = client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        job = await client.run(wf)
        logs = await job.get_logs()

    assert logs is not None
    assert logs.text == "got prompt\nPrompt executed in 4.62 seconds\n"
    assert logs.complete is True


async def test_async_get_logs_is_none_without_a_link(server) -> None:
    server.state.omit_logs_link = True
    async with AsyncComfy() as client:
        wf = client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        job = await client.run(wf)
        assert await job.get_logs() is None
    assert server.state.job_logs_request_count == 0


async def test_async_get_logs_is_none_when_the_server_answers_204(server) -> None:
    # The async half is hand-duplicated and the parity test compares names,
    # not behaviour, so the 204 branch needs its own async exercise or it is
    # only ever run on the sync side.
    server.state.job_logs = None
    async with AsyncComfy() as client:
        wf = client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})
        job = await client.run(wf)
        assert await job.get_logs() is None
    assert server.state.job_logs_request_count == 1
