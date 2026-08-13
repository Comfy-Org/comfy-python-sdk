"""Asset upload lifecycle: dedup fast-path, streaming upload, hash-mismatch."""

from __future__ import annotations

import io

import pytest

from comfy_low.errors import NotFound
from comfy_sdk import Comfy, HashMismatch


def test_dedup_fast_path_skips_upload(server, tmp_path) -> None:
    # Pre-seed the server with the blob the client is about to reference.
    data = b"already-on-the-server"
    p = tmp_path / "photo.png"
    p.write_bytes(data)

    with Comfy() as client:
        asset = client.assets.from_file(p)
        # Tell the server it already has these exact bytes (dedup hit).
        server.state.known_hashes.add(asset.hash)
        asset_id = asset.commit()

    assert asset_id == "asset_dedup_01"
    assert asset.created_new is False
    # HEAD probe hit, from-hash mint used, NO bytes uploaded.
    assert server.state.head_count == 1
    assert server.state.from_hash_count == 1
    assert server.state.upload_count == 0


class _ReadRecorder(io.BytesIO):
    """A file object that records the largest single read() it served."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.max_read = 0

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        chunk = super().read(size)
        # A size-less read() (buffer the whole file) would be recorded as huge.
        self.max_read = max(self.max_read, len(chunk))
        return chunk


def test_streaming_upload_does_not_buffer_whole_file(server, monkeypatch) -> None:
    # 1 MiB payload; the streaming multipart reads it in 64 KiB chunks.
    payload = b"x" * (1024 * 1024)
    recorder = _ReadRecorder(payload)

    with Comfy() as client:
        # from_stream buffers to hash, so drive the low transport directly with a
        # recording file object to observe how the body is read during upload.
        low = client._low
        low.post_assets(
            recorder,
            "application/octet-stream",
            "big.bin",
            expected_hash="blake3:deadbeef",
            file_size=len(payload),
        )

    assert server.state.upload_count == 1
    # Never read the whole file in one call — proves the body streamed.
    assert 0 < recorder.max_read <= 64 * 1024
    assert recorder.max_read < len(payload)


def test_post_assets_sends_one_multipart_part_per_tag(server, tmp_path) -> None:
    # Before the fix, `post_assets` built the multipart fields as a plain
    # `dict[str, str]` and used `setdefault("tags", t)` in a loop — so only the
    # FIRST tag ever made it into the request; the rest were silently dropped.
    payload = b"tagged-upload-bytes"
    p = tmp_path / "tagged.bin"
    p.write_bytes(payload)

    with Comfy() as client:
        with open(p, "rb") as fh:
            client._low.post_assets(
                fh,
                "application/octet-stream",
                "tagged.bin",
                tags=["a", "b", "c"],
            )

    body = server.state.last_upload_body
    assert body.count(b'name="tags"') == 3
    assert b'name="tags"\r\n\r\na\r\n' in body
    assert b'name="tags"\r\n\r\nb\r\n' in body
    assert b'name="tags"\r\n\r\nc\r\n' in body


def test_hash_mismatch_surfaced_without_blind_retry(server, tmp_path) -> None:
    server.state.reject_hash_mismatch = True
    p = tmp_path / "photo.png"
    p.write_bytes(b"some-bytes-that-will-mismatch")

    with Comfy() as client:
        asset = client.assets.from_file(p)
        with pytest.raises(HashMismatch):
            asset.commit()

    # Exactly one upload attempt — a 409 hash_mismatch must not be blindly retried.
    assert server.state.upload_count == 1


def test_delete_asset_by_id(server) -> None:
    with Comfy(server.base_url) as client:
        client.assets.delete("asset_uuid_01")
        with pytest.raises(NotFound):
            client.assets.get("asset_uuid_01")

    assert server.state.delete_count == 1


def test_delete_asset_on_asset_instance(server) -> None:
    data = b"delete-me-bytes"
    with Comfy(server.base_url) as client:
        asset = client.assets.from_bytes(data, filename="photo.png")
        asset.commit()
        asset_id = asset.id
        asset.delete()
        with pytest.raises(NotFound):
            client.assets.get(asset_id)

    assert asset_id == "asset_uploaded_01"
    assert server.state.delete_count == 1
    assert asset.id is None


def test_delete_uncommitted_asset_raises(server) -> None:
    with Comfy(server.base_url) as client:
        asset = client.assets.from_bytes(b"not-uploaded", filename="photo.png")
        with pytest.raises(RuntimeError, match="uncommitted"):
            asset.delete()

    assert server.state.delete_count == 0
