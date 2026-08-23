"""Range-aware download and core/ASSET substitution into the workflow graph."""

from __future__ import annotations

import io

import pytest

from comfy_sdk import Comfy
from comfy_sdk._core import find_asset_handles, substitute_asset_handles


def _wf(client: Comfy):
    return client.workflows.from_json({"3": {"class_type": "KSampler", "inputs": {}}})


class _ShortWriter(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data) -> int:
        return super().write(data[: self.limit])


def test_range_download_returns_partial(server) -> None:
    server.state.content_bytes = b"0123456789abcdef"
    with Comfy() as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        head = out.to_bytes(range=(0, 4))
    assert head == b"01234"  # bytes 0..4 inclusive


def test_range_download_to_file_writes_only_the_requested_slice(server, tmp_path) -> None:
    # `to_bytes(range=...)` is covered above; `to_file(range=...)` streams to
    # disk through a separate code path (chunked writes, not a bytearray) and
    # had no coverage at all.
    server.state.content_bytes = b"0123456789abcdef"
    with Comfy() as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        dest = out.to_file(tmp_path / "partial.bin", range=(4, 9))
    assert dest.read_bytes() == b"456789"  # bytes 4..9 inclusive


def test_full_download(server, tmp_path) -> None:
    with Comfy() as client:
        job = client.run(_wf(client))
        out = job.get_outputs("13")[0]
        dest = out.to_file(tmp_path / "out.bin")
    assert dest.read_bytes() == server.state.content_bytes


def test_to_stream_retries_partial_writes(server) -> None:
    stream = _ShortWriter(limit=3)
    with Comfy() as client:
        job = client.run(_wf(client))
        written = job.get_outputs("13")[0].to_stream(stream)
    assert written == len(server.state.content_bytes)
    assert stream.getvalue() == server.state.content_bytes


def test_to_stream_rejects_zero_byte_writes(server) -> None:
    stream = _ShortWriter(limit=0)
    with Comfy() as client:
        job = client.run(_wf(client))
        with pytest.raises(OSError, match="made no progress"):
            job.get_outputs("13")[0].to_stream(stream)


def test_core_asset_substitution(server, tmp_path) -> None:
    p = tmp_path / "photo.png"
    p.write_bytes(b"pixels")
    with Comfy() as client:
        server.state.known_hashes  # dedup not seeded -> will upload
        asset = client.assets.from_file(p)
        wf = client.workflows.from_json(
            {"10": {"class_type": "LoadImage", "inputs": {"image": asset}}}
        )
        # The handle is embedded in the raw graph until submit.
        assert find_asset_handles(wf.json) == [asset]

        client.submit(wf)

    # The server received a core/ASSET object in place of the handle.
    sent = server.state.last_workflow
    ref = sent["10"]["inputs"]["image"]
    assert ref["__type"] == "core/ASSET"
    assert ref["info"]["id"] == "asset_uploaded_01"


def test_substitute_leaves_plain_values_untouched() -> None:
    graph = {"3": {"inputs": {"seed": 42, "text": "hi", "nested": [1, {"a": "b"}]}}}
    out = substitute_asset_handles(graph, refs={})
    assert out == graph
