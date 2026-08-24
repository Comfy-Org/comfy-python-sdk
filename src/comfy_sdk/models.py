"""The ``models`` namespace — ``client.models`` on an existing client.

Reached from a client you already constructed (``Comfy().models`` /
``AsyncComfy().models``) rather than built on its own, so it uses that client's
credentials, base URL, transport and timeout: one connection pool, one
credential, one place to configure both. A separate client object for model
operations would fork all of that, which is what namespacing avoids.

The namespace holds the host client's transport itself — not a copy of its
settings — so a change made on the client after construction (a rotated key, a
different timeout) is visible through ``models`` with no re-wiring. v1 is the
namespace plus a read-only view of that shared configuration; model operations
are added to this object as they land, never to a parallel client.

Callers do not import anything for this: ``from comfy_sdk import Comfy`` stays
the only entry point, and ``client.models`` is the whole surface.
"""

from __future__ import annotations

import httpx

from comfy_low.transport import AsyncComfyLow, ComfyLow


class _ModelsBase:
    """Read-only view of the configuration inherited from the host client."""

    _low: ComfyLow | AsyncComfyLow

    @property
    def base_url(self) -> str:
        """The host client's base URL — where model requests are sent."""
        return self._low.base_url

    @property
    def timeout(self) -> httpx.Timeout:
        """The host client's HTTP timeout, read live from its transport."""
        return self._low.timeout

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"


class Models(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.Comfy`.

    Constructed by the client; ``low`` is the client's own transport, which is
    what makes the configuration shared rather than duplicated.
    """

    def __init__(self, low: ComfyLow) -> None:
        self._low = low


class AsyncModels(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.AsyncComfy` — mirrors :class:`Models`."""

    def __init__(self, low: AsyncComfyLow) -> None:
        self._low = low
