"""Job.get_workflow() / AsyncJob.get_workflow() — GET /api/v2/jobs/{id}/workflow.

This endpoint is not yet in spec/openapi.yaml (the server side is still in
review), so the stub server in conftest.py stands in for it directly rather
than the SDK talking to a generated model.
"""

from __future__ import annotations

import pytest

from comfy_sdk import AsyncComfy, Comfy, JobWorkflow, NotFound


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def test_get_workflow_returns_api_format(server) -> None:
    server.state.job_workflow_format = "api"
    server.state.job_workflow_graph = {"3": {"class_type": "KSampler", "inputs": {}}}
    with Comfy() as client:
        job = client.submit(_wf(client))
        wf = job.get_workflow()
    assert isinstance(wf, JobWorkflow)
    assert wf.format == "api"
    assert wf.graph == {"3": {"class_type": "KSampler", "inputs": {}}}


def test_get_workflow_returns_save_format(server) -> None:
    # "save" is the authoring workflow at the pinned version, un-mangled —
    # a different shape than the executed "api" graph, so both must be
    # reachable, not collapsed into one.
    server.state.job_workflow_format = "save"
    server.state.job_workflow_graph = {
        "nodes": [{"id": 3, "type": "KSampler"}],
        "links": [],
        "last_node_id": 3,
    }
    with Comfy() as client:
        job = client.submit(_wf(client))
        wf = job.get_workflow()
    assert wf.format == "save"
    assert wf.graph["nodes"][0]["type"] == "KSampler"


def test_get_workflow_on_job_rehydrated_by_id(server) -> None:
    # The motivating case: a job the SDK did not submit in this process (e.g.
    # rehydrated purely by id via client.jobs.get) still exposes its workflow.
    with Comfy() as client:
        submitted = client.submit(_wf(client))
        rehydrated = client.jobs.get(submitted.id)
        wf = rehydrated.get_workflow()
    assert wf.format == "api"


def test_get_workflow_not_found_raises_sdk_not_found(server) -> None:
    server.state.job_workflow_not_found = True
    with Comfy() as client:
        job = client.submit(_wf(client))
        with pytest.raises(NotFound):
            job.get_workflow()


async def test_async_get_workflow_mirrors_sync(server) -> None:
    server.state.job_workflow_format = "save"
    server.state.job_workflow_graph = {"nodes": [], "links": [], "last_node_id": 0}
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        wf = await job.get_workflow()
    assert wf.format == "save"
    assert wf.graph == {"nodes": [], "links": [], "last_node_id": 0}


async def test_async_get_workflow_not_found_raises_sdk_not_found(server) -> None:
    server.state.job_workflow_not_found = True
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        with pytest.raises(NotFound):
            await job.get_workflow()
