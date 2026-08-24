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

``run`` is the one model operation today, and it exists in exactly one form per
client: ``Comfy().models.run(...)`` blocks, ``AsyncComfy().models.run(...)`` is
awaited. **The awaitable form is the async client, not a suffixed method** —
there is deliberately no ``run_async``, and there never will be: two names for
one operation is a published signature that cannot be withdrawn once released.
``tests/test_models_run.py`` asserts the suffix's absence rather than leaving it
to convention.

Callers do not import anything for this: ``from comfy_sdk import Comfy`` stays
the only entry point, and ``client.models`` is the whole surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx

from comfy_low.transport import MODEL_RUN_TIMEOUT, AsyncComfyLow, ComfyLow

from ._core import new_idempotency_key
from .exceptions import translating


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
        # Redacted like the host client's repr: a base URL may carry proxy
        # credentials in its userinfo, and this namespace lands in the same
        # tracebacks and CI logs the client does.
        return f"{type(self).__name__}(base_url={self._low.safe_base_url!r})"


class Models(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.Comfy`.

    Constructed by the client; ``low`` is the client's own transport, which is
    what makes the configuration shared rather than duplicated.
    """

    def __init__(self, low: ComfyLow) -> None:
        self._low = low

    def run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """Run ``model`` with ``arguments`` and return the completed result.

        One call, one result. It blocks until the generation is finished —
        including for a provider the platform has to submit-and-poll, where the
        polling happens server side inside this call, invisible to the caller.
        There is no separate submit/await step and no ``run_async`` variant: the
        awaitable form of this method is :meth:`AsyncModels.run` on
        ``AsyncComfy``.

        The return value is the provider's own payload, decoded from JSON and
        handed back as-is — no wrapper class stands between the caller and the
        fields the provider documented.

        Because the server may legitimately hold the connection for minutes,
        ``timeout`` defaults to :data:`~comfy_low.transport.MODEL_RUN_TIMEOUT`
        (10 minutes) rather than the client's own default, which is sized for
        ordinary API calls and would abort a healthy run. Pass a number of
        seconds, an ``httpx.Timeout``, or ``None`` to wait indefinitely.

        An ``Idempotency-Key`` is sent on every run; a fresh one is minted per
        call unless ``idempotency_key`` is given, so an accidental exact resend
        is the server's to reject rather than a second charged generation.
        """
        low = cast(ComfyLow, self._low)
        with translating():
            return low.post_model_run(
                model,
                arguments,
                idempotency_key=idempotency_key or new_idempotency_key(),
                timeout=timeout,
            )


class AsyncModels(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.AsyncComfy` — mirrors :class:`Models`."""

    def __init__(self, low: AsyncComfyLow) -> None:
        self._low = low

    async def run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """Awaitable :meth:`Models.run` — same arguments, same result shape.

        This *is* the async form of ``run``: awaiting it on ``AsyncComfy`` is
        the whole difference from the sync client. See :meth:`Models.run`.
        """
        low = cast(AsyncComfyLow, self._low)
        with translating():
            return await low.post_model_run(
                model,
                arguments,
                idempotency_key=idempotency_key or new_idempotency_key(),
                timeout=timeout,
            )
