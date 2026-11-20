"""Streaming ``multipart/form-data`` body construction.

OpenAPI codegen routinely buffers an entire upload into memory to build the
request body; this module is the hand-written alternative the contract calls for.
The body is produced as a generator of byte chunks that reads the file part
lazily, and — when the file size is known — the exact Content-Length is computed
up front so the request streams instead of buffering.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import BinaryIO

CHUNK_SIZE = 64 * 1024


def _field_part(boundary: str, name: str, value: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()


def _file_header(boundary: str, name: str, filename: str, content_type: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()


def _closing(boundary: str) -> bytes:
    return f"--{boundary}--\r\n".encode()


def build_multipart(
    boundary: str,
    *,
    fields: list[tuple[str, str]],
    file_name: str,
    file_obj: BinaryIO,
    file_content_type: str,
    file_size: int | None,
    chunk_size: int = CHUNK_SIZE,
) -> tuple[Iterator[bytes], int | None]:
    """Return ``(body_iterator, content_length)``.

    ``fields`` is a list of ``(name, value)`` pairs rather than a ``dict`` so a
    repeatable field (e.g. ``tags``) can appear more than once — the standard
    multipart/form-data convention for sending a list — without one value
    clobbering another under the same key.

    ``content_length`` is ``None`` when ``file_size`` is unknown (the caller then
    lets the client fall back to chunked transfer encoding). The file object is
    read in ``chunk_size`` slices — never with a size-less ``read()`` — so a
    multi-GB file never lands in memory whole.
    """
    text_fields = b"".join(_field_part(boundary, name, value) for name, value in fields)
    file_hdr = _file_header(boundary, "file", file_name, file_content_type)
    closing = _closing(boundary)

    content_length: int | None = None
    if file_size is not None:
        content_length = len(text_fields) + len(file_hdr) + file_size + len(b"\r\n") + len(closing)

    def _iter() -> Iterator[bytes]:
        yield text_fields
        yield file_hdr
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            yield chunk
        yield b"\r\n"
        yield closing

    return _iter(), content_length


def file_size_of(file_obj: BinaryIO) -> int | None:
    """Best-effort remaining bytes of a seekable file object, else ``None``."""
    try:
        fd = file_obj.fileno()
    except (OSError, AttributeError):
        fd = None
    if fd is not None:
        try:
            return max(0, os.fstat(fd).st_size - file_obj.tell())
        except (OSError, AttributeError):
            pass
    try:
        cur = file_obj.tell()
        file_obj.seek(0, os.SEEK_END)
        end = file_obj.tell()
        file_obj.seek(cur, os.SEEK_SET)
        return max(0, end - cur)
    except (OSError, AttributeError):
        return None
