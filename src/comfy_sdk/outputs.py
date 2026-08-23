"""Output handles — typed, range-aware download over an asset id.

An output is an asset: the bytes are retrievable via ``getAssetContent`` (which
serves directly or ``302``-redirects to a signed URL) for as long as the job is
retained. ``to_file`` streams to disk in chunks; ``to_bytes`` buffers;
``get_download_url`` resolves a fetchable URL without transferring any bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import BinaryIO

from comfy_low.models import Output as LowOutput
from comfy_low.transport import AsyncComfyLow, ComfyLow

from .exceptions import translating

_CHUNK = 64 * 1024


def _write_all(stream: BinaryIO, chunk: bytes) -> int:
    """Write a complete chunk, including through partial writes."""
    written = 0
    while written < len(chunk):
        count = stream.write(chunk[written:])
        if count is None or count <= 0:
            raise OSError("stream.write() made no progress")
        if count > len(chunk) - written:
            raise OSError("stream.write() returned an invalid byte count")
        written += count
    return written


@dataclass(frozen=True)
class DownloadUrl:
    """A directly-fetchable URL for one output, e.g. to hand to a downstream
    consumer without streaming the bytes through the caller.

    ``url`` is a short-lived, self-authorizing bearer credential in its own
    right on backends that hand out signed URLs — whoever holds it can read
    the asset until ``expires_at``, no separate auth required.
    """

    url: str
    expires_at: datetime | None


class Output:
    """A single job output, downloadable via the sync transport."""

    def __init__(self, model: LowOutput, low: ComfyLow) -> None:
        self._model = model
        self._low = low

    @property
    def node_id(self) -> str:
        return self._model.node_id

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def type(self) -> str:
        return self._model.type.value

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def size_bytes(self) -> int:
        return self._model.size_bytes

    @property
    def content_type(self) -> str:
        return self._model.content_type

    @property
    def job_id(self) -> str | None:
        """The id of the job that produced this output, or ``None`` if the
        backend didn't report one."""
        return self._model.job_id

    def to_file(self, path: str | PathLike[str], *, range: tuple[int, int] | None = None) -> Path:
        """Stream this output to ``path`` and return the path written.

        Bytes are written in chunks as they arrive, so an output larger than
        memory is fine. ``range=(first, last)`` requests only that byte slice,
        inclusive of both ends, matching HTTP ``Range: bytes=first-last`` —
        ``range=(0, 4)`` yields the first five bytes.
        """
        dest = Path(path)
        with translating(), self._low.get_asset_content(self._model.id, range=range) as resp:
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)
        return dest

    def to_stream(self, stream: BinaryIO, *, range: tuple[int, int] | None = None) -> int:
        """Copy this output into an already-open binary ``stream``.

        Returns the number of bytes written. The stream is written to but never
        closed — that stays the caller's. See :meth:`to_file` for ``range``.
        """
        written = 0
        with translating(), self._low.get_asset_content(self._model.id, range=range) as resp:
            for chunk in resp.iter_bytes(_CHUNK):
                written += _write_all(stream, chunk)
        return written

    def to_bytes(self, *, range: tuple[int, int] | None = None) -> bytes:
        """Return this output's bytes in memory.

        Convenient for small outputs; prefer :meth:`to_file` or
        :meth:`to_stream` for large ones, since this buffers the whole body.
        See :meth:`to_file` for ``range``.
        """
        buf = bytearray()
        with translating(), self._low.get_asset_content(self._model.id, range=range) as resp:
            for chunk in resp.iter_bytes(_CHUNK):
                buf.extend(chunk)
        return bytes(buf)

    def get_download_url(self) -> DownloadUrl:
        """A directly-fetchable URL for this output.

        On a Cloud/serverless backend this is a short-lived, self-authorizing
        signed URL for object storage: anyone holding it can read the bytes
        until ``expires_at``. On a self-hosted backend it is the content
        endpoint itself (the same auth as every other call still applies), and
        ``expires_at`` is ``None``. (A genuine failure — e.g. an unknown output
        id — still raises the same typed error as any other call.)
        """
        with translating():
            url, expires_at = self._low.get_asset_content_url(self._model.id)
        return DownloadUrl(url=url, expires_at=expires_at)

    def __repr__(self) -> str:
        return f"Output(node_id={self.node_id!r}, name={self.name!r}, id={self.id!r})"


class AsyncOutput:
    """A single job output, downloadable via the async transport."""

    def __init__(self, model: LowOutput, low: AsyncComfyLow) -> None:
        self._model = model
        self._low = low

    @property
    def node_id(self) -> str:
        return self._model.node_id

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def type(self) -> str:
        return self._model.type.value

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def size_bytes(self) -> int:
        return self._model.size_bytes

    @property
    def content_type(self) -> str:
        return self._model.content_type

    @property
    def job_id(self) -> str | None:
        """The id of the job that produced this output, or ``None`` if the
        backend didn't report one."""
        return self._model.job_id

    async def to_file(
        self, path: str | PathLike[str], *, range: tuple[int, int] | None = None
    ) -> Path:
        """Async :meth:`Output.to_file` — same chunked write and inclusive ``range``."""
        dest = Path(path)
        with translating():
            async with self._low.get_asset_content(self._model.id, range=range) as resp:
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        fh.write(chunk)
        return dest

    async def to_stream(self, stream: BinaryIO, *, range: tuple[int, int] | None = None) -> int:
        """Async :meth:`Output.to_stream` — same write-only semantics."""
        written = 0
        with translating():
            async with self._low.get_asset_content(self._model.id, range=range) as resp:
                async for chunk in resp.aiter_bytes(_CHUNK):
                    written += _write_all(stream, chunk)
        return written

    async def to_bytes(self, *, range: tuple[int, int] | None = None) -> bytes:
        """Async :meth:`Output.to_bytes` — buffers the whole body in memory."""
        buf = bytearray()
        with translating():
            async with self._low.get_asset_content(self._model.id, range=range) as resp:
                async for chunk in resp.aiter_bytes(_CHUNK):
                    buf.extend(chunk)
        return bytes(buf)

    async def get_download_url(self) -> DownloadUrl:
        """See the sync ``Output.get_download_url`` for the redirect/inline split."""
        with translating():
            url, expires_at = await self._low.get_asset_content_url(self._model.id)
        return DownloadUrl(url=url, expires_at=expires_at)

    def __repr__(self) -> str:
        return f"AsyncOutput(node_id={self.node_id!r}, name={self.name!r}, id={self.id!r})"
