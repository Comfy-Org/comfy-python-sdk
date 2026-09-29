"""Model discovery on Comfy Router — ``models.list()`` and ``models.schema()``.

The two read-only routes that tell a caller *what* it can run before it runs
anything: the catalog (``GET /v2/models``, ``listRouterModels``) and one model's
input/output schema as a standalone OpenAPI document
(``GET /v2/models/{provider}/{model}/openapi.json``,
``getRouterModelInputSchema``). Both go to the same host, with the same
credential, as :meth:`comfy_sdk.models.Models.run`, and a failure raises the
same typed :class:`~comfy_sdk.router_exceptions.RouterError` subclass, read off
``X-Comfy-Error-Type``.

The shapes mirror the TypeScript SDK's ``comfy.models.list()`` /
``comfy.models.schema()`` so the two surfaces answer the same question the same
way:

* ``list()`` returns a :class:`ModelList` — iterate it to walk every page,
  following ``next_cursor`` while ``has_more`` is true, or call
  :meth:`ModelList.page` for exactly one page and its paging facts;
* ``schema()`` returns a :class:`SchemaResult`. With ``etag=`` the request is
  conditional, and a ``304`` comes back as ``unchanged=True`` rather than as an
  exception, because "nothing changed" is the answer the caller asked for.
  Storing the tag between calls is the caller's business; the SDK keeps no
  cache.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import httpx

from comfy_low.errors import ApiError, clean_request_id
from comfy_low.transport import DISCOVERY_TIMEOUT, AsyncComfyLow, ComfyLow

from .exceptions import ComfyError, translating
from .retry import Retrier, RetryPolicy
from .router_exceptions import REQUEST_ID_HEADER, RouterError

#: See :data:`comfy_sdk.models._CANDIDATE_FAILURES` — restated rather than
#: imported because :mod:`comfy_sdk.models` imports this module.
_CANDIDATE_FAILURES = (ApiError, RouterError, httpx.TransportError)

_now = time.monotonic

_T = TypeVar("_T")

Timeout = float | httpx.Timeout | None


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """One entry in the Router model catalog — the identity of a runnable model."""

    id: str
    """The canonical ``{provider}/{model}`` id — pass it straight to ``models.run``."""

    provider: str
    """The ``provider`` segment of :attr:`id`."""

    model: str
    """The ``model`` segment of :attr:`id`."""

    billing: Mapping[str, Any]
    """Per-model billing facts a caller needs before invoking — never prices.

    Carried as the decoded JSON object (today it holds
    ``charges_on_policy_rejection``) rather than as a class, so a field the
    server adds reaches the caller without an SDK release.
    """


@dataclass(frozen=True, slots=True)
class ModelPage:
    """One page of the Router model catalog, as :meth:`ModelList.page` returns it."""

    data: tuple[CatalogModel, ...]
    """The models on this page, at most :attr:`limit` of them."""

    has_more: bool
    """Whether another page exists. Walk on this, never on a short :attr:`data`."""

    next_cursor: str | None
    """Pass as ``cursor=`` to fetch the next page; ``None`` on the last one."""

    limit: int
    """The page size the server actually served.

    A requested ``limit`` above the maximum (100) is clamped down rather than
    rejected, so this can be smaller than the value asked for.
    """

    request_id: str | None
    """``X-Comfy-Request-Id`` — the id to quote in a support request."""


@dataclass(frozen=True, slots=True)
class SchemaResult:
    """What :meth:`comfy_sdk.models.Models.schema` returns.

    ``unchanged`` is the branch to take: ``False`` carries the full
    :attr:`document`; ``True`` means the ``etag`` passed in still matches, the
    server sent no body, and :attr:`document` is ``None``.
    """

    unchanged: bool
    """``True`` on a ``304`` — the document is the one the caller's ``etag`` names."""

    document: dict[str, Any] | None
    """The input/output schemas as a standalone OpenAPI document; ``None`` on a ``304``."""

    etag: str | None
    """``ETag`` of the current document — send it back as ``etag=`` next time."""

    request_id: str | None
    """``X-Comfy-Request-Id`` — the id to quote in a support request."""


def _invalid(message: str, request_id: str | None) -> ComfyError:
    return ComfyError(message, code="invalid_response", request_id=request_id)


def _entry(raw: Any, request_id: str | None) -> CatalogModel:
    """One catalog entry, or ``invalid_response`` if it cannot address a run.

    ``id``/``provider``/``model`` are required and checked, because they are
    what a caller hands back to ``models.run``; ``billing`` is read leniently,
    because nothing downstream is addressed by it.
    """
    if not isinstance(raw, dict):
        raise _invalid("catalog entry is not a JSON object", request_id)
    fields = (raw.get("id"), raw.get("provider"), raw.get("model"))
    if not all(isinstance(value, str) and value for value in fields):
        raise _invalid("catalog entry is missing id, provider or model", request_id)
    billing = raw.get("billing")
    return CatalogModel(
        id=cast(str, fields[0]),
        provider=cast(str, fields[1]),
        model=cast(str, fields[2]),
        billing=dict(billing) if isinstance(billing, dict) else {},
    )


def _page(body: dict[str, Any], headers: Mapping[str, str]) -> ModelPage:
    request_id = clean_request_id(headers.get(REQUEST_ID_HEADER))
    data = body.get("data")
    has_more = body.get("has_more")
    limit = body.get("limit")
    if not isinstance(data, list) or not isinstance(has_more, bool):
        raise _invalid("catalog page is missing data or has_more", request_id)
    next_cursor = body.get("next_cursor")
    return ModelPage(
        data=tuple(_entry(item, request_id) for item in data),
        has_more=has_more,
        next_cursor=next_cursor if isinstance(next_cursor, str) and next_cursor else None,
        # `limit` is required by the contract, but a page that omits it is
        # still a usable page; its own length is the honest stand-in.
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else len(data),
        request_id=request_id,
    )


def _next_cursor(page: ModelPage, seen: set[str]) -> str | None:
    """The cursor to walk to next, or ``None`` once the catalog is exhausted.

    A page that says ``has_more`` yet names no cursor, or names one this walk
    already followed, would otherwise loop forever re-reading the same page;
    both are raised as ``invalid_response`` instead.
    """
    if not page.has_more:
        return None
    cursor = page.next_cursor
    if cursor is None:
        raise _invalid("catalog page says has_more but names no next_cursor", page.request_id)
    if cursor in seen:
        raise _invalid("catalog walk was handed a cursor it already followed", page.request_id)
    seen.add(cursor)
    return cursor


def _schema_result(body: dict[str, Any] | None, headers: Mapping[str, str]) -> SchemaResult:
    return SchemaResult(
        unchanged=body is None,
        document=body,
        etag=headers.get("ETag"),
        request_id=clean_request_id(headers.get(REQUEST_ID_HEADER)),
    )


def _call(policy: RetryPolicy, send: Callable[[], _T]) -> _T:
    """Run one discovery read under the client's retry policy, translated."""
    retrier = Retrier(policy, now=_now)
    with translating():
        while True:
            try:
                return send()
            except _CANDIDATE_FAILURES as exc:
                delay = retrier.delay_before_retry(exc)
                if delay is None:
                    raise
                time.sleep(delay)


async def _acall(policy: RetryPolicy, send: Callable[[], Awaitable[_T]]) -> _T:
    """Awaitable :func:`_call`."""
    retrier = Retrier(policy, now=_now)
    with translating():
        while True:
            try:
                return await send()
            except _CANDIDATE_FAILURES as exc:
                delay = retrier.delay_before_retry(exc)
                if delay is None:
                    raise
                await asyncio.sleep(delay)


class ModelList:
    """The Router model catalog, returned by :meth:`comfy_sdk.models.Models.list`.

    Iterating it walks every page from ``cursor`` (the first page when
    omitted), yielding :class:`CatalogModel` entries and following
    ``next_cursor`` while ``has_more`` is true. Nothing is fetched until
    iteration starts, and each ``for`` loop starts a fresh walk. Call
    :meth:`page` instead for exactly one page and its paging facts.
    """

    def __init__(
        self,
        low: ComfyLow,
        retry: RetryPolicy,
        *,
        cursor: str | None,
        limit: int | None,
        timeout: Timeout,
    ) -> None:
        self._low = low
        self._retry = retry
        self._cursor = cursor
        self._limit = limit
        self._timeout = timeout

    def _fetch(self, cursor: str | None) -> ModelPage:
        body, headers = _call(
            self._retry,
            lambda: self._low.get_model_catalog(
                cursor=cursor, limit=self._limit, timeout=self._timeout
            ),
        )
        return _page(body, headers)

    def page(self) -> ModelPage:
        """Fetch one page — the one ``cursor`` names — and return it whole."""
        return self._fetch(self._cursor)

    def __iter__(self) -> Iterator[CatalogModel]:
        cursor = self._cursor
        seen: set[str] = set() if cursor is None else {cursor}
        while True:
            page = self._fetch(cursor)
            yield from page.data
            cursor = _next_cursor(page, seen)
            if cursor is None:
                return


class AsyncModelList:
    """The Router model catalog on ``AsyncComfy`` — mirrors :class:`ModelList`.

    ``async for`` walks every page; ``await catalog.page()`` fetches one.
    """

    def __init__(
        self,
        low: AsyncComfyLow,
        retry: RetryPolicy,
        *,
        cursor: str | None,
        limit: int | None,
        timeout: Timeout,
    ) -> None:
        self._low = low
        self._retry = retry
        self._cursor = cursor
        self._limit = limit
        self._timeout = timeout

    async def _fetch(self, cursor: str | None) -> ModelPage:
        body, headers = await _acall(
            self._retry,
            lambda: self._low.get_model_catalog(
                cursor=cursor, limit=self._limit, timeout=self._timeout
            ),
        )
        return _page(body, headers)

    async def page(self) -> ModelPage:
        """Awaitable :meth:`ModelList.page`."""
        return await self._fetch(self._cursor)

    async def __aiter__(self) -> AsyncIterator[CatalogModel]:
        cursor = self._cursor
        seen: set[str] = set() if cursor is None else {cursor}
        while True:
            page = await self._fetch(cursor)
            for entry in page.data:
                yield entry
            cursor = _next_cursor(page, seen)
            if cursor is None:
                return


def get_schema(
    low: ComfyLow, retry: RetryPolicy, model: str, *, etag: str | None, timeout: Timeout
) -> SchemaResult:
    """One ``openapi.json`` read, sync — see :meth:`comfy_sdk.models.Models.schema`."""
    body, headers = _call(retry, lambda: low.get_model_schema(model, etag=etag, timeout=timeout))
    return _schema_result(body, headers)


async def aget_schema(
    low: AsyncComfyLow, retry: RetryPolicy, model: str, *, etag: str | None, timeout: Timeout
) -> SchemaResult:
    """Awaitable :func:`get_schema`."""
    body, headers = await _acall(
        retry, lambda: low.get_model_schema(model, etag=etag, timeout=timeout)
    )
    return _schema_result(body, headers)


__all__ = [
    "DISCOVERY_TIMEOUT",
    "AsyncModelList",
    "CatalogModel",
    "ModelList",
    "ModelPage",
    "SchemaResult",
]
