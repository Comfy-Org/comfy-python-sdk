"""The ``models`` namespace — ``client.models`` on an existing client.

Reached from a client you already constructed (``Comfy().models`` /
``AsyncComfy().models``) rather than built on its own, so it uses that client's
credentials, transport and timeout: one connection pool, one credential, one
place to configure both. A separate client object for model operations would
fork all of that, which is what namespacing avoids.

The one setting it does *not* share is the target host. Model runs go to Comfy
Router — ``POST {router_base_url}/v2/models/{provider}/{model}``, the partner
model's native JSON straight through — while the client's ``base_url`` names
the ``/api/v2`` deployment serving jobs and assets. So ``models.base_url``
reports ``COMFY_ROUTER_BASE_URL`` (default ``https://api.comfy.org``), and a
``model`` argument is the canonical two-segment ``{provider}/{model}`` id that
addresses the route.

The namespace holds the host client's transport itself — not a copy of its
settings — so a change made on the client after construction (a rotated key, a
different timeout) is visible through ``models`` with no re-wiring. v1 is the
namespace plus a read-only view of that shared configuration; model operations
are added to this object as they land, never to a parallel client.

The namespace also carries the client's retry policy, for the same reason it
carries its transport: a retry is part of how a call is made, not a per-call
decision a caller should have to repeat. See :mod:`comfy_sdk.retry` for what is
retried and why one logical call keeps one ``Idempotency-Key`` across every
attempt.

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

import asyncio
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

import httpx

from comfy_low.errors import ApiError
from comfy_low.transport import MODEL_RUN_TIMEOUT, AsyncComfyLow, ComfyLow

from ._core import new_idempotency_key, validate_idempotency_key
from .exceptions import translating
from .retry import DEFAULT_RETRY, Retrier, RetryPolicy
from .router_exceptions import RouterError

#: Failures the policy is even asked about. A protocol ``ApiError`` is a
#: response the server sent, a ``RouterError`` is the same thing modelled by
#: this surface's own typed hierarchy, and an ``httpx.TransportError`` is no
#: response at all. Being in this tuple is not "retryable" — most of these are
#: not, and :meth:`RetryPolicy.should_retry` decides which. What it buys is
#: that anything *else* propagates untouched, so a bug in the SDK itself is
#: never mistaken for a flaky network and quietly re-run.
#:
#: ``RouterError`` is listed even though ``post_model_run`` raises ``ApiError``
#: today: the typed errors in :mod:`comfy_sdk.router_exceptions` were built for
#: this very surface and derive from ``ComfyError``, not ``ApiError``, so
#: omitting them would make :meth:`RetryPolicy.should_retry`'s router branch
#: unreachable and turn retry into a silent no-op the day this route starts
#: raising them, with no test failing.
_CANDIDATE_FAILURES = (ApiError, RouterError, httpx.TransportError)

_now = time.monotonic


class _ModelsBase:
    """Read-only view of the configuration inherited from the host client."""

    _low: ComfyLow | AsyncComfyLow
    _retry: RetryPolicy

    @property
    def base_url(self) -> str:
        """Comfy Router's base URL — where model requests are sent.

        Not the host client's ``base_url``: a model run is a model-ID-addressed
        route on Comfy Router (``https://api.comfy.org`` by default, redirected
        by ``COMFY_ROUTER_BASE_URL``), while the client's own ``base_url`` names
        the ``/api/v2`` deployment that serves jobs and assets. Read live off
        the shared transport, exactly as :attr:`timeout` is.
        """
        return self._low.router_base_url

    @property
    def timeout(self) -> httpx.Timeout:
        """The host client's HTTP timeout, read live from its transport."""
        return self._low.timeout

    @property
    def retry(self) -> RetryPolicy:
        """The host client's retry policy — what a failed attempt is worth."""
        return self._retry

    def __repr__(self) -> str:
        # Redacted like the host client's repr: a base URL may carry proxy
        # credentials in its userinfo, and this namespace lands in the same
        # tracebacks and CI logs the client does. It renders the *router* base
        # URL because that is the target this namespace actually talks to —
        # showing the client's `/api/v2` base URL here would name a host no
        # `models` call ever reaches.
        return f"{type(self).__name__}(base_url={self._low.safe_router_base_url!r})"


class Models(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.Comfy`.

    Constructed by the client; ``low`` is the client's own transport, which is
    what makes the configuration shared rather than duplicated.
    """

    def __init__(self, low: ComfyLow, retry: RetryPolicy = DEFAULT_RETRY) -> None:
        self._low = low
        self._retry = retry

    def run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        """Run ``model`` with ``arguments`` and return the completed result.

        ``model`` is the canonical ``{provider}/{model}`` id — exactly the two
        path segments that address the run on Comfy Router
        (``POST {router_base_url}/v2/models/{provider}/{model}``), and exactly
        what the model catalog lists. It must be two non-empty segments: a
        one-segment id, the three-segment ``{provider}/{model}/{variant}`` form
        (not addressable on this route yet), and any ``.``/``..`` segment each
        raise ``ValueError`` locally, before any request is made; a non-string
        raises ``TypeError``.

        ``arguments`` is the partner model's **own native JSON input**,
        forwarded to the provider unchanged — there is no Comfy-shaped envelope
        around it, so whatever the partner documents as its request body is
        what goes here.

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
        is the server's to deduplicate rather than a second charged generation.

        A failed attempt is retried under the client's policy, backed off with
        jitter and bounded by total elapsed time. **Every attempt of this one
        call reuses the one key**, which is what stops a retry from being
        billed as a second generation; calling ``run`` again is a new call and
        mints a new key. A key presented twice is *not* re-run: on the router
        surface a resend of the same key with the same body collects the
        generation the first request started rather than dispatching another,
        and a resend with a *different* body is refused ``409``
        ``invalid_input`` (the contract says to use a NEW key) — which is why
        the body is snapshotted before the first attempt. (The single-use, reject-on-duplicate rule
        ``spec/openapi.yaml`` states is the **v2 jobs API**'s, and governs
        ``submit()``, not this route.)

        Retried by default: connect-phase failures, a ``429`` that names a
        ``Retry-After``, and the answers that name a pace for collecting work
        already running — a ``deadline_exceeded`` ``504`` and an in-flight-key
        ``concurrency_limit_exceeded`` ``409``, each carrying ``Retry-After``. One
        ``run()`` can therefore ride the collect loop through a server-side
        deadline to the finished generation. Not retried by default: a completed
        5xx that named no such pace, and a client-side timeout — those leave the
        outcome genuinely unknown and need
        ``RetryPolicy(retry_possibly_in_flight=True)``. See
        :mod:`comfy_sdk.retry`, and ``Comfy(retry=NO_RETRY)`` to switch it off.

        **Every exception this raises carries the key it sent** on
        ``.idempotency_key`` (and the server's ``.request_id`` when the response
        named one), so a caller who lost the response — a ``deadline_exceeded``
        ``504``, a dropped connection — can still collect a generation they were
        already billed for: ``client.models.run(model, arguments,
        idempotency_key=exc.idempotency_key)`` returns the original result
        (``200`` + ``Idempotent-Replayed``) against a deployment that replays a
        claimed key, or is refused while that generation is still running, with
        ``exc.retry_after`` naming when to ask again when the server sent a
        ``Retry-After``. Without that attribute an auto-minted key died with the
        call and the paid-for generation was uncollectable.

        Note that a dropped connection surfaces as an ``httpx`` error rather
        than a :class:`~comfy_sdk.ComfyError` — it never reached a response to
        translate — so a handler written for the replay has to catch both; see
        the README's "Collecting a generation after a lost response".

        An explicit key must be unique across the WHOLE workspace, not just
        within your own client: the keyspace is shared by every member of the
        workspace your credential carries, so a second caller reusing a stable
        label like ``"retry-1"`` is answered from the first caller's record —
        or refused ``409`` if the request differs. Mint keys with real entropy
        (the default does); never derive them from guessable labels.

        Pass ``exc.idempotency_key`` back only after checking it is not
        ``None``. This parameter treats ``None`` as "mint one", so replaying
        with a key that was never recorded silently starts a *second* billed
        generation instead of collecting the first. Every exception *this*
        method raises carries a real key, but an ``except ComfyError`` that also
        catches errors from other surfaces can hand you one that does not.
        """
        low = cast(ComfyLow, self._low)
        # Minted once, outside the loop: reusing this exact value on every
        # attempt is the whole reason a retry here is not a second charge.
        key = (
            validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else new_idempotency_key()
        )
        # Snapshotted for the same reason the key is. Re-reading the caller's
        # mapping inside each attempt would let a mutation between attempts
        # send a different body under the *same* key, which is precisely the
        # same-key-different-body case the contract rejects outright.
        #
        # Deep, not shallow: a shallow copy leaves every nested list and dict
        # shared with the caller, so mutating `arguments["config"]["steps"]`
        # during the retry window would still change the body under the one key
        # and earn the 422 this snapshot exists to prevent. The body is JSON on
        # the wire, so everything legal in it is deep-copyable.
        payload = deepcopy(dict(arguments))
        retrier = Retrier(self._retry, now=_now)
        # The key is stamped onto whatever this raises: it is a local of this
        # frame, so an exception that propagates past it would otherwise take
        # the caller's only route back to an already-billed generation with it.
        with translating(idempotency_key=key):
            while True:
                try:
                    return low.post_model_run(model, payload, idempotency_key=key, timeout=timeout)
                except _CANDIDATE_FAILURES as exc:
                    delay = retrier.delay_before_retry(exc)
                    if delay is None:
                        raise
                    time.sleep(delay)


class AsyncModels(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.AsyncComfy` — mirrors :class:`Models`."""

    def __init__(self, low: AsyncComfyLow, retry: RetryPolicy = DEFAULT_RETRY) -> None:
        self._low = low
        self._retry = retry

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
        the whole difference from the sync client — including the model-id
        rule, the retry policy, the one-key-per-call rule, and the
        ``.idempotency_key`` every exception it raises carries for the replay.
        See :meth:`Models.run`.
        """
        low = cast(AsyncComfyLow, self._low)
        key = (
            validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else new_idempotency_key()
        )
        # Snapshotted deeply before the first attempt — see :meth:`Models.run`.
        # The window is wider here: the retry sleeps inside the caller's own
        # event loop, so another task is free to run and mutate `arguments`,
        # nested values included.
        payload = deepcopy(dict(arguments))
        retrier = Retrier(self._retry, now=_now)
        # The key is stamped onto whatever this raises: it is a local of this
        # frame, so an exception that propagates past it would otherwise take
        # the caller's only route back to an already-billed generation with it.
        with translating(idempotency_key=key):
            while True:
                try:
                    return await low.post_model_run(
                        model, payload, idempotency_key=key, timeout=timeout
                    )
                except _CANDIDATE_FAILURES as exc:
                    delay = retrier.delay_before_retry(exc)
                    if delay is None:
                        raise
                    await asyncio.sleep(delay)
