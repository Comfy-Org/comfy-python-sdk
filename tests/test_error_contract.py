"""The ``translating()`` contract: no public entry point may leak the
protocol-level ``comfy_low.errors.ApiError`` — every error must surface as its
``comfy_sdk`` typed equivalent (see ``comfy_sdk.exceptions.translating``).

Regression guard for the 10 entry points that used to skip it: the four
``outputs.py`` download methods (sync + async), both asset/job factories'
``get()``, and the non-501 raise in ``events()``.

The second half of the file pins the *shape* of that surface: the attributes
every SDK exception is guaranteed to answer to. They are asserted here rather
than left to whichever raise site happens to set them, because an attribute a
caller reads inside an ``except`` block is a published contract the moment the
package ships — a later release cannot take one away, and one that is present
on some errors and missing on others forces every caller into ``getattr``.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import comfy_low.transport as low_transport
from comfy_low.errors import ApiError as LowApiError
from comfy_sdk import AsyncComfy, Comfy, Forbidden, NotFound
from comfy_sdk.exceptions import ComfyError, translating
from comfy_sdk.router_exceptions import REQUEST_ID_HEADER, ROUTER_EXCEPTIONS, RouterError


def _wf(client: Comfy | AsyncComfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


def _assert_no_leak(entry_point: str, fn: Callable[[], Any], expected: type[ComfyError]) -> None:
    try:
        fn()
    except ComfyError as exc:
        assert isinstance(exc, expected), (
            f"{entry_point} raised {type(exc).__name__}, expected {expected.__name__}"
        )
    except LowApiError as exc:
        pytest.fail(
            f"{entry_point} leaked comfy_low.errors.{type(exc).__name__} "
            "instead of translating it to a comfy_sdk ComfyError"
        )
    else:
        pytest.fail(f"{entry_point} did not raise; the scenario should force an error")


async def _assert_no_leak_async(
    entry_point: str, coro: Awaitable[Any], expected: type[ComfyError]
) -> None:
    try:
        await coro
    except ComfyError as exc:
        assert isinstance(exc, expected), (
            f"{entry_point} raised {type(exc).__name__}, expected {expected.__name__}"
        )
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

        _assert_no_leak("Output.to_bytes", out.to_bytes, NotFound)
        _assert_no_leak("Output.to_file", lambda: out.to_file(tmp_path / "o.bin"), NotFound)
        _assert_no_leak("Output.to_stream", lambda: out.to_stream(io.BytesIO()), NotFound)
        _assert_no_leak("Output.get_download_url", out.get_download_url, NotFound)


async def test_async_output_download_methods_translate_on_deleted_asset(server, tmp_path) -> None:
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        out = job.get_outputs("13")[0]
        await client.assets.delete(out.id)

        await _assert_no_leak_async("AsyncOutput.to_bytes", out.to_bytes(), NotFound)
        await _assert_no_leak_async(
            "AsyncOutput.to_file", out.to_file(tmp_path / "o.bin"), NotFound
        )
        await _assert_no_leak_async("AsyncOutput.to_stream", out.to_stream(io.BytesIO()), NotFound)
        await _assert_no_leak_async(
            "AsyncOutput.get_download_url", out.get_download_url(), NotFound
        )


# -- AssetFactory.get / AsyncAssetFactory.get --------------------------------


def test_asset_factory_get_translates_on_deleted_asset(server) -> None:
    with Comfy() as client:
        client.assets.delete("asset_out_01")
        _assert_no_leak("AssetFactory.get", lambda: client.assets.get("asset_out_01"), NotFound)


async def test_async_asset_factory_get_translates_on_deleted_asset(server) -> None:
    async with AsyncComfy() as client:
        await client.assets.delete("asset_out_01")
        await _assert_no_leak_async(
            "AsyncAssetFactory.get", client.assets.get("asset_out_01"), NotFound
        )


# -- JobFactory.get / AsyncJobFactory.get ------------------------------------


def test_job_factory_get_translates_on_missing_job(server) -> None:
    server.state.job_not_found = True
    with Comfy() as client:
        _assert_no_leak("JobFactory.get", lambda: client.jobs.get("no_such_job"), NotFound)


async def test_async_job_factory_get_translates_on_missing_job(server) -> None:
    server.state.job_not_found = True
    async with AsyncComfy() as client:
        await _assert_no_leak_async("AsyncJobFactory.get", client.jobs.get("no_such_job"), NotFound)


# -- Job.events() / AsyncJob.events() non-501 raise --------------------------


def test_job_events_translates_non_501_error(server) -> None:
    with Comfy() as client:
        job = client.run(_wf(client))
        server.state.events_error = (403, "forbidden")
        _assert_no_leak("Job.events", lambda: list(job.events()), Forbidden)


async def test_async_job_events_translates_non_501_error(server) -> None:
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        server.state.events_error = (403, "forbidden")

        async def _drain() -> None:
            async for _ in job.events():
                pass

        await _assert_no_leak_async("AsyncJob.events", _drain(), Forbidden)


# -- the attributes every SDK exception answers to ---------------------------


#: The whole published attribute surface of the base error. Spelled out rather
#: than introspected so that *adding* one is a deliberate edit to this list —
#: which is what makes it reviewable — and *removing* one is a failing test
#: rather than a silent break of somebody's ``except`` block.
_BASE_ATTRIBUTES = (
    "message",
    "code",
    "http_status",
    "details",
    "request_id",
    "idempotency_key",
)


@pytest.mark.parametrize("name", _BASE_ATTRIBUTES)
def test_the_base_error_answers_to_its_whole_attribute_surface(name: str) -> None:
    assert hasattr(ComfyError("boom"), name)


@pytest.mark.parametrize("name", ("request_id", "idempotency_key"))
def test_those_attributes_default_to_none_rather_than_being_absent(name: str) -> None:
    # An operation that sends no Idempotency-Key, and a response that named no
    # request id, both leave the attribute readable as `None`. A caller writes
    # `exc.idempotency_key` unconditionally; there is no surface on which that
    # is an AttributeError.
    assert getattr(ComfyError("boom"), name) is None


@pytest.mark.parametrize("cls", ROUTER_EXCEPTIONS + (RouterError,), ids=lambda c: c.__name__)
def test_every_router_bucket_answers_to_the_key_including_the_base(cls: type) -> None:
    # Including `RouterError` itself, which is what an `error_type` this SDK
    # version has never heard of falls through to.
    assert cls("detail", http_status=500).idempotency_key is None


def test_translating_stamps_the_key_onto_a_translated_protocol_error() -> None:
    with pytest.raises(NotFound) as excinfo:
        with translating(idempotency_key="k-01"):
            raise LowApiError("gone", code="not_found", http_status=404)
    assert excinfo.value.idempotency_key == "k-01"


def test_translating_stamps_the_key_onto_an_sdk_error_it_did_not_build() -> None:
    with pytest.raises(RouterError) as excinfo:
        with translating(idempotency_key="k-02"):
            raise RouterError("nope", error_type="unheard_of", http_status=503)
    assert excinfo.value.idempotency_key == "k-02"


def test_translating_without_a_key_leaves_the_attribute_alone() -> None:
    # The parameter is additive: every existing caller passes nothing and gets
    # exactly the behaviour it had before the parameter existed.
    with pytest.raises(NotFound) as excinfo:
        with translating():
            raise LowApiError("gone", code="not_found", http_status=404)
    assert excinfo.value.idempotency_key is None


def test_a_bug_in_the_sdk_is_not_stamped_and_not_swallowed() -> None:
    # `_STAMPABLE` is deliberately not "everything": a programming error is not
    # a failed call, a key means nothing on it, and it must reach the caller
    # unaltered. Same reasoning as `models._CANDIDATE_FAILURES`.
    with pytest.raises(ZeroDivisionError) as excinfo:
        with translating(idempotency_key="k-03"):
            raise ZeroDivisionError("a bug, not a failed request")
    assert not hasattr(excinfo.value, "idempotency_key")


def test_the_two_layers_spell_the_request_id_header_the_same_way() -> None:
    # `comfy_low` reads it off the shared error envelope and `comfy_sdk` reads
    # it off a router error response, so the name is written out in both — and
    # neither may import the other's copy (comfy_low never imports comfy_sdk).
    # A drift here would silently drop the id on one of the two surfaces.
    assert low_transport.REQUEST_ID_HEADER == REQUEST_ID_HEADER
