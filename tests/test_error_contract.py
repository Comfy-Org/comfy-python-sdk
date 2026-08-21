"""The ``translating()`` contract: no public entry point may leak the
protocol-level ``comfy_low.errors.ApiError`` — every error must surface as its
``comfy_sdk`` typed equivalent (see ``comfy_sdk.exceptions.translating``).

Regression guard for the 10 entry points that used to skip it: the four
``outputs.py`` download methods (sync + async), both asset/job factories'
``get()``, and the non-501 raise in ``events()``.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from comfy_low.errors import ApiError as LowApiError
from comfy_sdk import AsyncComfy, Comfy
from comfy_sdk.exceptions import ComfyError


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def _assert_no_leak(entry_point: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except ComfyError:
        return
    except LowApiError as exc:
        pytest.fail(
            f"{entry_point} leaked comfy_low.errors.{type(exc).__name__} "
            "instead of translating it to a comfy_sdk ComfyError"
        )
    else:
        pytest.fail(f"{entry_point} did not raise; the scenario should force an error")


async def _assert_no_leak_async(entry_point: str, coro: Awaitable[Any]) -> None:
    try:
        await coro
    except ComfyError:
        return
    except LowApiError as exc:
        pytest.fail(
            f"{entry_point} leaked comfy_low.errors.{type(exc).__name__} "
            "instead of translating it to a comfy_sdk ComfyError"
        )
    else:
        pytest.fail(f"{entry_point} did not raise; the scenario should force an error")


# -- outputs.py: to_file / to_stream / to_bytes / get_download_url ----------


def test_output_download_methods_translate_on_deleted_asset(server, tmp_path) -> None:
    with Comfy() as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        client.assets.delete(out.id)  # server now 404s this asset's content

        _assert_no_leak("Output.to_bytes", out.to_bytes)
        _assert_no_leak("Output.to_file", lambda: out.to_file(tmp_path / "o.bin"))
        _assert_no_leak("Output.to_stream", lambda: out.to_stream(io.BytesIO()))
        _assert_no_leak("Output.get_download_url", out.get_download_url)


async def test_async_output_download_methods_translate_on_deleted_asset(server, tmp_path) -> None:
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        out = job.get_outputs("13")[0]
        await client.assets.delete(out.id)

        await _assert_no_leak_async("AsyncOutput.to_bytes", out.to_bytes())
        await _assert_no_leak_async("AsyncOutput.to_file", out.to_file(tmp_path / "o.bin"))
        await _assert_no_leak_async("AsyncOutput.to_stream", out.to_stream(io.BytesIO()))
        await _assert_no_leak_async("AsyncOutput.get_download_url", out.get_download_url())


# -- AssetFactory.get / AsyncAssetFactory.get --------------------------------


def test_asset_factory_get_translates_on_deleted_asset(server) -> None:
    with Comfy() as client:
        client.assets.delete("asset_out_01")
        _assert_no_leak("AssetFactory.get", lambda: client.assets.get("asset_out_01"))


async def test_async_asset_factory_get_translates_on_deleted_asset(server) -> None:
    async with AsyncComfy() as client:
        await client.assets.delete("asset_out_01")
        await _assert_no_leak_async("AsyncAssetFactory.get", client.assets.get("asset_out_01"))


# -- JobFactory.get / AsyncJobFactory.get ------------------------------------


def test_job_factory_get_translates_on_missing_job(server) -> None:
    server.state.job_not_found = True
    with Comfy() as client:
        _assert_no_leak("JobFactory.get", lambda: client.jobs.get("no_such_job"))


async def test_async_job_factory_get_translates_on_missing_job(server) -> None:
    server.state.job_not_found = True
    async with AsyncComfy() as client:
        await _assert_no_leak_async("AsyncJobFactory.get", client.jobs.get("no_such_job"))


# -- Job.events() / AsyncJob.events() non-501 raise --------------------------


def test_job_events_translates_non_501_error(server) -> None:
    with Comfy() as client:
        job = client.run(_wf(client))
        server.state.events_error = (403, "forbidden")
        _assert_no_leak("Job.events", lambda: list(job.events()))


async def test_async_job_events_translates_non_501_error(server) -> None:
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        server.state.events_error = (403, "forbidden")

        async def _drain() -> None:
            async for _ in job.events():
                pass

        await _assert_no_leak_async("AsyncJob.events", _drain())
