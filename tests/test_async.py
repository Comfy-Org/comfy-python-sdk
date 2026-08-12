"""The async client mirrors the sync surface against the same stub server."""

from __future__ import annotations

import pytest

from comfy_sdk import AsyncComfy, MissingAsset, Progress, StatusChange


def _wf(client: AsyncComfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


async def test_async_run_and_download(server, tmp_path) -> None:
    server.state.polls_to_succeed = 2
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        assert job.status == "succeeded"
        out = job.get_outputs("13")[0]
        data = await out.to_bytes()
    assert data == server.state.content_bytes


async def test_async_events_stream_to_terminal(server) -> None:
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [e async for e in job.events()]
    assert isinstance(seen[-1], StatusChange)
    assert seen[-1].status == "succeeded"


async def test_async_sse_reconnect_with_no_replay(server) -> None:
    # Mirrors `test_sse_reconnect_with_no_replay` in test_events.py, but for
    # `AsyncJob.events()` — only the sync reconnect path had coverage before.
    # The first stream drops after one progress frame with no terminal status;
    # the async client must reconnect (fresh live frames, nothing replayed).
    server.state.sse_mode = "reconnect"
    server.state.polls_to_succeed = 1000
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        seen = [e async for e in job.events()]

    # Two physical connections were made to the events endpoint.
    assert server.state.events_connect_count == 2
    assert isinstance(seen[-1], StatusChange)
    assert seen[-1].status == "succeeded"
    # No replay: one progress frame per connection, not a replayed backlog.
    progresses = [e for e in seen if isinstance(e, Progress)]
    assert len(progresses) == 2


async def test_async_range_download_returns_partial(server) -> None:
    # The sync client has range-download coverage (test_download_and_workflows.py);
    # the async `to_bytes(range=...)` path was never exercised.
    server.state.content_bytes = b"0123456789abcdef"
    async with AsyncComfy() as client:
        job = await client.run(_wf(client))
        out = job.get_outputs("13")[0]
        head = await out.to_bytes(range=(0, 4))
    assert head == b"01234"  # bytes 0..4 inclusive


async def test_async_dedup_fast_path(server, tmp_path) -> None:
    p = tmp_path / "photo.png"
    p.write_bytes(b"async-dedup-bytes")
    async with AsyncComfy() as client:
        asset = client.assets.from_file(p)
        server.state.known_hashes.add(asset.hash)
        asset_id = await asset.commit()
    assert asset_id == "asset_dedup_01"
    assert server.state.upload_count == 0


async def test_async_real_upload_of_fresh_file_succeeds(server, tmp_path) -> None:
    # Deliberately do NOT seed `known_hashes` — the dedup HEAD probe misses, so
    # `commit()` must drive a real multipart upload over the AsyncClient. Before
    # the fix, `AsyncComfyLow.post_assets` handed httpx a *sync* generator body,
    # which raises RuntimeError ("Attempted to send a sync request with an
    # AsyncClient instance") the moment httpx tries to send it.
    p = tmp_path / "fresh.bin"
    p.write_bytes(b"a fresh, never-before-seen payload that forces a real upload")
    async with AsyncComfy() as client:
        asset = client.assets.from_file(p)
        asset_id = await asset.commit()
    assert asset_id == "asset_uploaded_01"
    assert asset.created_new is True
    assert server.state.upload_count == 1
    assert server.state.from_hash_count == 0


async def test_async_error_mapping(server) -> None:
    server.state.job_error = (422, "missing_asset")
    async with AsyncComfy() as client:
        with pytest.raises(MissingAsset):
            await client.submit(_wf(client))


async def test_async_cancel_reaches_server(server) -> None:
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        await job.cancel()
        assert job.status == "canceling"


async def test_async_wait_raises_timeout(server) -> None:
    server.state.polls_to_succeed = 10_000
    async with AsyncComfy() as client:
        job = await client.submit(_wf(client))
        with pytest.raises(TimeoutError):
            await job.wait(timeout=0.05)


async def test_async_core_asset_substitution(server, tmp_path) -> None:
    # The async commit -> mint -> substitute pipeline, mirroring the sync test.
    p = tmp_path / "photo.png"
    p.write_bytes(b"pixels")
    async with AsyncComfy() as client:
        asset = client.assets.from_file(p)
        wf = client.workflows.from_json(
            {"10": {"class_type": "LoadImage", "inputs": {"image": asset}}}
        )
        await client.submit(wf)
    ref = server.state.last_workflow["10"]["inputs"]["image"]
    assert ref["__type"] == "core/ASSET"
    assert ref["info"]["id"] == "asset_uploaded_01"


async def test_async_submit_with_api_key_sends_extra_data(server) -> None:
    # Mirrors the sync `test_submit_with_api_key_sends_extra_data_sibling_of_workflow`.
    async with AsyncComfy() as client:
        await client.submit(_wf(client), api_key="comfyui-secret-key")
    body = server.state.last_jobs_body
    assert body is not None
    assert body["extra_data"] == {"api_key_comfy_org": "comfyui-secret-key"}


async def test_async_submit_without_api_key_omits_extra_data(server) -> None:
    async with AsyncComfy() as client:
        await client.submit(_wf(client))
    body = server.state.last_jobs_body
    assert body is not None
    assert "extra_data" not in body


async def test_async_submit_with_embed_workflow_embeds_materialized_graph(server) -> None:
    # Mirrors the sync `test_submit_with_embed_workflow_embeds_materialized_graph`.
    async with AsyncComfy() as client:
        await client.submit(_wf(client), embed_workflow=True)
    body = server.state.last_jobs_body
    assert body is not None
    assert body["extra_data"] == {"extra_pnginfo": {"workflow": body["workflow"]}}


async def test_async_submit_with_embed_workflow_and_api_key_merges_extra_data(server) -> None:
    async with AsyncComfy() as client:
        await client.submit(_wf(client), api_key="comfyui-secret-key", embed_workflow=True)
    body = server.state.last_jobs_body
    assert body is not None
    assert body["extra_data"] == {
        "api_key_comfy_org": "comfyui-secret-key",
        "extra_pnginfo": {"workflow": body["workflow"]},
    }


async def test_async_run_forwards_embed_workflow_to_submit(server) -> None:
    async with AsyncComfy() as client:
        await client.run(_wf(client), embed_workflow=True)
    body = server.state.last_jobs_body
    assert body is not None
    assert body["extra_data"] == {"extra_pnginfo": {"workflow": body["workflow"]}}


async def test_async_queue_full_retries_with_retry_after(server) -> None:
    server.state.queue_full_times = 2  # 429 twice, then 201
    async with AsyncComfy() as client:
        await client.submit(_wf(client))
    assert server.state.submit_count == 3
