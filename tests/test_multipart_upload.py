"""Real multipart uploads use the remaining bytes of a caller-owned stream."""

from __future__ import annotations

import io
from email.parser import BytesParser
from email.policy import default

import pytest

from comfy_low import AsyncComfyLow, ComfyLow

_PAYLOAD = b"abcdefghij"


@pytest.fixture(params=["disk", "memory"])
def upload_stream(request, tmp_path):
    if request.param == "memory":
        stream = io.BytesIO(_PAYLOAD)
    else:
        path = tmp_path / "source.bin"
        path.write_bytes(_PAYLOAD)
        stream = open(path, "rb")
    with stream:
        yield stream


def _uploaded_file(body):
    boundary = body.split(b"\r\n", 1)[0][2:]
    envelope = b"Content-Type: multipart/form-data; boundary=" + boundary + b"\r\n\r\n" + body
    message = BytesParser(policy=default).parsebytes(envelope)
    return next(
        part.get_payload(decode=True)
        for part in message.iter_parts()
        if part.get_param("name", header="content-disposition") == "file"
    )


@pytest.mark.parametrize("offset", [0, 4, 10, 14])
def test_sync_upload_from_current_position(server, upload_stream, offset):
    upload_stream.read(min(offset, len(_PAYLOAD)))
    if offset > len(_PAYLOAD):
        upload_stream.seek(offset)
    with ComfyLow(server.base_url) as client:
        result = client.post_assets(upload_stream, "application/octet-stream", "source.bin")
    assert result.id == "asset_uploaded_01"
    assert not upload_stream.closed
    assert server.state.upload_count == 1
    assert _uploaded_file(server.state.last_upload_body) == _PAYLOAD[offset:]


@pytest.mark.parametrize("offset", [0, 4, 10, 14])
async def test_async_upload_from_current_position(server, upload_stream, offset):
    upload_stream.read(min(offset, len(_PAYLOAD)))
    if offset > len(_PAYLOAD):
        upload_stream.seek(offset)
    async with AsyncComfyLow(server.base_url) as client:
        result = await client.post_assets(upload_stream, "application/octet-stream", "source.bin")
    assert result.id == "asset_uploaded_01"
    assert not upload_stream.closed
    assert server.state.upload_count == 1
    assert _uploaded_file(server.state.last_upload_body) == _PAYLOAD[offset:]
