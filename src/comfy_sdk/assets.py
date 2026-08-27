"""Content-addressed asset handles and their constructors.

``client.assets.from_*`` returns a **lazy** :class:`Asset` handle: no network at
construction. On first use (``commit``/``as_reference``/submit) the handle hashes
its bytes locally with blake3, probes the server's dedup fast-path
(``HEAD by-hash`` then ``from-hash`` mint over existing bytes), and only streams a
full ``multipart`` upload when the server does not already have the bytes. Because
the handle carries an idempotency key, re-running a script re-uploads nothing
whose bytes already made it to the server.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from os import PathLike
from os.path import basename, getsize
from typing import BinaryIO

import httpx

from comfy_low.models import Asset as LowAsset
from comfy_low.transport import AsyncComfyLow, ComfyLow

from . import _core, _hashing
from .exceptions import translating

Opener = Callable[[], "tuple[BinaryIO, int | None]"]
Hasher = Callable[[], str]

_DEFAULT_CT = "application/octet-stream"


@dataclass
class _Source:
    """A resolved byte source: how to hash it and how to open it for upload."""

    content_type: str
    file_path: str
    hasher: Hasher
    opener: Opener
    expires_in: int | None = None


def _guess_content_type(name: str | None) -> str:
    if name:
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            return guessed
    return _DEFAULT_CT


class _AssetBase:
    _is_comfy_asset = True

    def __init__(self, source: _Source) -> None:
        self._content_type = source.content_type
        self._file_path = source.file_path
        self._hasher = source.hasher
        self._opener = source.opener
        self._expires_in = source.expires_in
        self._hash: str | None = None
        self._id: str | None = None
        self._created_new: bool | None = None
        self._url: str | None = None
        self._job_id: str | None = None
        self._expires_at: datetime | None = None
        self._idempotency_key = _core.new_idempotency_key()

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def file_path(self) -> str:
        return self._file_path

    @property
    def hash(self) -> str:
        """The local blake3 (computed once, lazily)."""
        if self._hash is None:
            self._hash = self._hasher()
        return self._hash

    @property
    def created_new(self) -> bool | None:
        return self._created_new

    @property
    def job_id(self) -> str | None:
        """The id of the job that produced this asset, or ``None`` for an
        asset with no producing job (e.g. a plain upload)."""
        return self._job_id

    @property
    def expires_at(self) -> datetime | None:
        """Retention deadline for this asset, or ``None`` if it doesn't
        expire."""
        return self._expires_at

    def _apply(self, asset: LowAsset) -> None:
        self._id = asset.id
        if asset.hash:
            self._hash = asset.hash
        self._created_new = asset.created_new
        self._url = str(asset.url)
        self._job_id = asset.job_id
        self._expires_at = asset.expires_at

    def __repr__(self) -> str:
        state = self._id or "uncommitted"
        return f"{type(self).__name__}(file_path={self._file_path!r}, {state})"


class Asset(_AssetBase):
    """A lazy asset handle bound to the synchronous client."""

    def __init__(self, low: ComfyLow, source: _Source) -> None:
        super().__init__(source)
        self._low = low

    def commit(self) -> str:
        """Force the hash/dedup/upload now; return the asset UUID."""
        if self._id is not None:
            return self._id
        digest = self.hash
        with translating():
            if self._low.head_asset_by_hash(digest):
                asset = self._low.asset_from_hash(
                    digest, file_path=self._file_path, expires_in=self._expires_in
                )
            else:
                fh, size = self._opener()
                try:
                    asset = self._low.post_assets(
                        fh,
                        self._content_type,
                        self._file_path,
                        expected_hash=digest,
                        idempotency_key=self._idempotency_key,
                        file_size=size,
                        expires_in=self._expires_in,
                    )
                finally:
                    fh.close()
        self._apply(asset)
        assert self._id is not None
        return self._id

    def delete(self) -> None:
        """Delete this asset from storage."""
        if self._id is None:
            raise RuntimeError("cannot delete an uncommitted asset")
        with translating():
            self._low.delete_asset(self._id)
        self._id = None

    def as_reference(self) -> dict[str, object]:
        """The ``core/ASSET`` object (commits first if needed)."""
        self.commit()
        assert self._id is not None
        return _core.asset_reference(self._id, hash=self._hash, file_path=self._file_path)


class AsyncAsset(_AssetBase):
    """A lazy asset handle bound to the asynchronous client."""

    def __init__(self, low: AsyncComfyLow, source: _Source) -> None:
        super().__init__(source)
        self._low = low

    async def commit(self) -> str:
        if self._id is not None:
            return self._id
        digest = self.hash
        with translating():
            if await self._low.head_asset_by_hash(digest):
                asset = await self._low.asset_from_hash(
                    digest, file_path=self._file_path, expires_in=self._expires_in
                )
            else:
                fh, size = self._opener()
                try:
                    asset = await self._low.post_assets(
                        fh,
                        self._content_type,
                        self._file_path,
                        expected_hash=digest,
                        idempotency_key=self._idempotency_key,
                        file_size=size,
                        expires_in=self._expires_in,
                    )
                finally:
                    fh.close()
        self._apply(asset)
        assert self._id is not None
        return self._id

    async def delete(self) -> None:
        """Delete this asset from storage."""
        if self._id is None:
            raise RuntimeError("cannot delete an uncommitted asset")
        with translating():
            await self._low.delete_asset(self._id)
        self._id = None

    async def as_reference(self) -> dict[str, object]:
        await self.commit()
        assert self._id is not None
        return _core.asset_reference(self._id, hash=self._hash, file_path=self._file_path)


# ---- source builders (shared, sans-IO except explicit reads) ------------


def _file_source(path: str | PathLike[str], *, expires_in: int | None = None) -> _Source:
    p = str(path)
    name = basename(p)
    return _Source(
        content_type=_guess_content_type(name),
        file_path=name,
        hasher=lambda: _hashing.hash_file(p),
        opener=lambda: (open(p, "rb"), getsize(p)),
        expires_in=expires_in,
    )


def _bytes_source(data: bytes, filename: str | None, content_type: str | None) -> _Source:
    name = filename or "upload.bin"
    return _Source(
        content_type=content_type or _guess_content_type(filename),
        file_path=name,
        hasher=lambda: _hashing.hash_bytes(data),
        opener=lambda: (BytesIO(data), len(data)),
    )


class AssetFactory:
    """``client.assets`` — sync alternative constructors for :class:`Asset`."""

    def __init__(self, low: ComfyLow) -> None:
        self._low = low

    def from_file(self, path: str | PathLike[str], *, expires_in: int | None = None) -> Asset:
        return Asset(self._low, _file_source(path, expires_in=expires_in))

    def from_bytes(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> Asset:
        return Asset(self._low, _bytes_source(data, filename, content_type))

    def from_stream(
        self,
        stream: BinaryIO,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> Asset:
        # A stream needs a full pass to hash; buffer it so the dedup probe can run
        # before upload and the bytes can be re-read for the upload itself.
        data = stream.read()
        return Asset(self._low, _bytes_source(data, filename, content_type))

    def from_url(self, url: str) -> Asset:
        # Client-side download (not a server-side fetch): the bytes must transit
        # the same blake3 -> dedup -> upload pipeline as every other source.
        resp = httpx.get(url, follow_redirects=True)
        resp.raise_for_status()
        filename = basename(httpx.URL(url).path) or "download.bin"
        ct = resp.headers.get("Content-Type", "").split(";")[0] or None
        return Asset(self._low, _bytes_source(resp.content, filename, ct))

    def get(self, asset_id: str) -> Asset:
        """Rehydrate an already-committed asset by UUID."""
        with translating():
            model = self._low.get_asset(asset_id)
        asset = Asset(self._low, _rehydrated_source(model, asset_id))
        asset._apply(model)
        return asset

    def delete(self, asset_id: str) -> None:
        """Delete an asset by UUID."""
        with translating():
            self._low.delete_asset(asset_id)


class AsyncAssetFactory:
    """``client.assets`` — async alternative constructors for :class:`AsyncAsset`."""

    def __init__(self, low: AsyncComfyLow) -> None:
        self._low = low

    def from_file(self, path: str | PathLike[str], *, expires_in: int | None = None) -> AsyncAsset:
        return AsyncAsset(self._low, _file_source(path, expires_in=expires_in))

    def from_bytes(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> AsyncAsset:
        return AsyncAsset(self._low, _bytes_source(data, filename, content_type))

    def from_stream(
        self,
        stream: BinaryIO,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> AsyncAsset:
        data = stream.read()
        return AsyncAsset(self._low, _bytes_source(data, filename, content_type))

    async def from_url(self, url: str) -> AsyncAsset:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
        filename = basename(httpx.URL(url).path) or "download.bin"
        ct = resp.headers.get("Content-Type", "").split(";")[0] or None
        return AsyncAsset(self._low, _bytes_source(content, filename, ct))

    async def get(self, asset_id: str) -> AsyncAsset:
        with translating():
            model = await self._low.get_asset(asset_id)
        asset = AsyncAsset(self._low, _rehydrated_source(model, asset_id))
        asset._apply(model)
        return asset

    async def delete(self, asset_id: str) -> None:
        """Delete an asset by UUID."""
        with translating():
            await self._low.delete_asset(asset_id)


def _no_opener() -> tuple[BinaryIO, int | None]:
    raise RuntimeError("this asset is already committed; nothing to upload")


def _rehydrated_source(model: LowAsset, asset_id: str) -> _Source:
    """A source for an already-committed asset (rehydrated by UUID): it never
    uploads, so the opener is a guard.
    """
    return _Source(
        content_type=model.content_type,
        file_path=model.file_path or asset_id,
        hasher=lambda: model.hash or "",
        opener=_no_opener,
    )
