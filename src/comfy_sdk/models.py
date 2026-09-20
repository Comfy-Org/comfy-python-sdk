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
the only entry point, and ``client.models`` is the whole surface. The one name
worth importing is :class:`~comfy_low.transport.BinaryResult`, re-exported here
and from ``comfy_sdk``, for an ``isinstance`` check on a run whose model answers
in bytes rather than JSON.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

import httpx

from comfy_low.errors import ApiError
from comfy_low.errors import IdempotencyKeyReuse as ProtocolIdempotencyKeyReuse
from comfy_low.transport import (
    MODEL_RUN_TIMEOUT,
    AsyncComfyLow,
    BinaryResult,
    ComfyLow,
    parse_model_id,
    parse_request_id,
)

from ._core import new_idempotency_key, validate_idempotency_key
from .exceptions import IdempotencyKeyReuse, _stamp, to_sdk_error, translating
from .model_requests import (
    _CANCEL_FAILURES,
    _CANCEL_TIMEOUT,
    AsyncRequestHandle,
    QueueUpdate,
    RequestHandle,
    _completed,
    _remaining,
    _request_id_of,
)
from .retry import DEFAULT_RETRY, Retrier, RetryPolicy, may_have_claimed_key
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

#: The answer that means the resend was never going to work: the server refused
#: the *key*, not the request. ``422`` is the v2 jobs rule
#: (single-use, reject-on-duplicate), which a deployment named by
#: ``COMFY_ROUTER_BASE_URL`` may apply to this route even though the router
#: contract does not.
_KEY_REUSE_STATUS = 422
_KEY_REUSE_CODE = "idempotency_key_reuse"

_now = time.monotonic


def _is_key_reuse(exc: BaseException) -> bool:
    """Whether ``exc`` is the server refusing a repeated ``Idempotency-Key``.

    Matched on the wire facts — ``422`` plus the ``idempotency_key_reuse``
    code — rather than on a class alone, because the retry loop runs *inside*
    ``translating()``: what it catches is the raw
    :class:`comfy_low.errors.ApiError`, and only the copy that leaves the block
    is the idiomatic :class:`~comfy_sdk.exceptions.IdempotencyKeyReuse`. Either
    layer's typed class is accepted outright so the answer does not depend on
    which one raised.

    The SDK-level :class:`~comfy_sdk.exceptions.IdempotencyKeyReuse` arm is
    deliberately defensive, not reachable from the retry loops as written:
    that class derives from ``ComfyError`` alone, so the ``except
    _CANDIDATE_FAILURES`` clauses that call this can only ever bind the
    protocol copy. It is kept so this predicate stays correct if it is ever
    called from outside those clauses.
    """
    if isinstance(exc, (IdempotencyKeyReuse, ProtocolIdempotencyKeyReuse)):
        return True
    return (
        getattr(exc, "http_status", None) == _KEY_REUSE_STATUS
        and getattr(exc, "code", None) == _KEY_REUSE_CODE
    )


def _as_sdk_error(exc: BaseException, idempotency_key: str) -> BaseException:
    """``exc`` on the surface a caller catches, carrying ``idempotency_key``.

    ``translating()`` converts and stamps the exception it is *handed*, and
    nothing else — so an ``ApiError`` chained onto that one as ``__cause__``
    would reach the caller as a raw protocol type this SDK otherwise never
    shows. Both halves of the substitution below therefore go through here
    first. Anything already idiomatic (a ``RouterError``, an ``httpx`` failure
    with no response to translate) is only stamped.

    The translated copy inherits the original's traceback, so the chain still
    points at the attempt that produced each half rather than at the one line
    of this module that re-raised them.
    """
    if not isinstance(exc, ApiError):
        return _stamp(exc, idempotency_key)
    return _stamp(to_sdk_error(exc).with_traceback(exc.__traceback__), idempotency_key)


def _resend_refused(exc: BaseException) -> BaseException:
    """Mark ``exc`` as the failure whose same-key resend the server refused.

    Set on the error that is raised, never on the ``__cause__``. An outer retry
    wrapper keyed on ``http_status`` alone cannot tell this 5xx apart from a
    fresh one, and re-entering :meth:`Models.run` mints a *new* key — the second
    billed generation. The flag is the cheap way to tell them apart without
    walking ``__cause__``; see
    :attr:`comfy_sdk.exceptions.ComfyError.resend_refused`.
    """
    exc.resend_refused = True  # type: ignore[attr-defined]
    return exc


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


@dataclass(frozen=True, slots=True)
class RouterRunResult:
    """One finished model run, plus what Router disclosed about HOW it ran.

    :meth:`Models.run` returns the partner model's native output on its own,
    which is the right default: that document is the thing a caller asked for,
    and wrapping every run in an envelope to carry two usually-absent headers
    would tax every caller for the few that need them. This is the opt-in shape
    for the callers that do -- :meth:`Models.run_detailed`.

    What it adds is not decoration. On an alt-provider run
    (``model_provider=...``) the response body is translated back to this
    model's native contract, so the body alone looks IDENTICAL whether the call
    was served by the model's own provider or by an alternate. The two headers
    below are the only disclosure of the difference, which makes them the only
    way a caller -- or a test -- can prove which leg actually ran.
    """

    output: dict[str, Any] | BinaryResult
    """The partner model's native output, exactly what :meth:`Models.run` returns.

    A ``dict`` for a model whose partner answers JSON, and a
    :class:`~comfy_sdk.BinaryResult` for one whose partner answers a generation
    directly as bytes — the same two shapes, decided the same way, as
    :meth:`Models.run`. ``run_detailed`` adds the Router disclosures beside the
    output; it does not change what the output is.
    """

    serving_provider: str | None
    """``X-Comfy-Router-Fallback-Provider``: the provider that ultimately served this call.

    Present ONLY when ``fallback_provider`` retried against a second provider
    and that retry succeeded -- never a provider that was attempted and also
    failed. ``None`` therefore means "the provider asked for served it", which
    is the common case; it does not mean "unknown".
    """

    dropped_params: tuple[str, ...] | None
    """``X-Comfy-Router-Dropped-Params``: native fields translation could not carry.

    Present only when ``model_provider`` translated the body
    (``strict_mode=False``, the default) and one or more native fields could not
    be expressed exactly on the alternate provider's schema; each entry names the
    field and why. ``None`` when no translation ran or it dropped nothing.
    """

    replayed: bool
    """``X-Comfy-Idempotent-Replayed``: served from the key's record, not run again.

    A replay is not billed a second time. The header is absent on a fresh run
    rather than sent as ``false``, so this is derived from its presence.
    """

    request_id: str | None
    """``X-Comfy-Request-Id`` -- the id to quote in a support request."""


def _dropped_params(raw: str | None) -> tuple[str, ...] | None:
    """Parse the ``X-Comfy-Router-Dropped-Params`` header value.

    The spec describes this header as "a JSON array of strings" in prose while
    declaring ``schema: {type: array, items: {type: string}}``, which in OpenAPI
    means the SIMPLE comma-delimited form instead. The two disagree, and the
    spec's own example settles which one the server actually sends: its single
    entry is ``moderation (fal applies its own, non-configurable safety
    filtering)`` -- which contains a comma. Splitting on commas would tear that
    one entry into two meaningless fragments, so the prose is right and the
    declared schema is the part that is wrong.

    So: parse as JSON, and fall back to the raw value as a single entry rather
    than guessing at delimiters. The fallback is deliberately not a comma split
    -- on a malformed value, one intact entry a human can read beats two
    confident fragments. (The spec bug is filed separately against the server's
    own openapi.yml, which this vendored copy is synced from; correcting it
    here would be reverted by the next sync.)
    """
    if raw is None:
        return None
    with contextlib.suppress(ValueError, TypeError):
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return tuple(parsed)
    return (raw,)


def _run_result(
    body: dict[str, Any] | BinaryResult, headers: Mapping[str, str]
) -> RouterRunResult:
    """Build a :class:`RouterRunResult` from one run's body and response headers."""
    return RouterRunResult(
        output=body,
        serving_provider=headers.get("X-Comfy-Router-Fallback-Provider"),
        dropped_params=_dropped_params(headers.get("X-Comfy-Router-Dropped-Params")),
        replayed=headers.get("X-Comfy-Idempotent-Replayed") is not None,
        request_id=headers.get("X-Comfy-Request-Id"),
    )


class Models(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.Comfy`.

    Constructed by the client; ``low`` is the client's own transport, which is
    what makes the configuration shared rather than duplicated.
    """

    def __init__(self, low: ComfyLow, retry: RetryPolicy = DEFAULT_RETRY) -> None:
        self._low = low
        self._retry = retry

    def _run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        strict_mode: bool | None = None,
        fallback_provider: bool | str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> tuple[dict[str, Any] | BinaryResult, Mapping[str, str]]:
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

        ``model_provider`` selects an alternate serving provider for this model
        (Comfy Router's ``model_provider`` query param); omitted, the model runs
        on its default provider and the request is byte-for-byte what it always
        was. Under the default ``strict_mode`` (``False``) the ``arguments`` you
        pass stay this model's own native input, and Router translates them to
        the alternate provider's schema on the way in and the response back to
        native on the way out; ``strict_mode=True`` sends and returns that
        provider's own raw shape unchanged, so no translation happens either
        way. ``fallback_provider`` controls the retry-against-another-provider
        behavior — pass ``False`` (or ``"false"``) to opt out. Each is sent only
        when set. Note that the off switch is the only value that means
        anything: Router reads *any* other value, and omission, as fallback ON,
        which is why a ``bool`` here is normalised to ``"true"``/``"false"``
        rather than str()-ed into the capitalised ``"False"`` that would read as
        "on".

        One call, one result. It blocks until the generation is finished —
        including for a provider the platform has to submit-and-poll, where the
        polling happens server side inside this call, invisible to the caller.
        There is no separate submit/await step and no ``run_async`` variant: the
        awaitable form of this method is :meth:`AsyncModels.run` on
        ``AsyncComfy``.

        The return value is the provider's own payload, handed back as-is — no
        wrapper class stands between the caller and what the provider produced.
        It comes in **two shapes**, decided by the response's ``Content-Type``,
        because Router forwards the partner's output under the partner's own
        media type:

        * a ``dict`` — the provider's JSON document, decoded, with its own field
          names untouched. This is what all but a couple of models in the
          catalog return, and it is unchanged from previous releases.
        * a :class:`~comfy_low.transport.BinaryResult` — for a model whose
          partner answers a generation directly as bytes (the ElevenLabs audio
          models are the first of these). ``result.content`` is the file bytes
          exactly as they arrived, ``result.content_type`` the media type the
          response named, ``result.request_id`` its ``X-Comfy-Request-Id``. The
          bytes are not base64-encoded and not wrapped in a dict: write them to
          a file and you have the file the partner produced.

        Branch with ``isinstance(result, BinaryResult)`` when you call a model
        that might do either; a model's own contract (``GET
        /v2/models/{provider}/{model}/openapi.json``) says which it is, and a
        ``200`` whose ``Content-Type`` claims JSON but whose body will not parse
        is still an error rather than bytes.

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

        **When a resend is refused because the key was already claimed
        (``422`` ``idempotency_key_reuse``), what is raised is the failure that
        claimed it** — the ``504``, the ``500`` — with the key refusal chained
        onto it as ``__cause__`` and ``.resend_refused`` set to ``True``. That
        refusal is an artefact of this retry loop rather than an answer about
        the request, so surfacing it in place of the real failure would hide the
        only error worth diagnosing. Every other terminal failure is raised
        exactly as the server sent it, first or last.

        Two things narrow which failure that is, and both exist so the
        substitution never buries a *genuine* key refusal:

        * Only a failure that **could have claimed the key** is eligible. A
          never-delivered transport failure (``ConnectError``, ``ConnectTimeout``,
          ``PoolTimeout``, ``ProxyError``) never reached a server, and a ``429``
          is rejected without starting work and explicitly releases the key — so
          a ``422`` following one of those is the server refusing a key that was
          consumed somewhere else entirely, and it is raised as itself. Without
          that gate a caller-supplied key already spent elsewhere would surface
          as a transport blip, and a wrapper retrying transport blips would loop
          forever on a key that can never succeed.
        * Among eligible failures the **most recent** is kept, not the first.
          In a mixed run — a ``429`` that started nothing, then a ``504`` that
          left a generation running, then the ``422`` — the ``504`` is the one
          that describes the state the refusal implies. Raising the ``429``
          would tell the caller nothing was started and invite a fresh-key
          retry, which is the second billed generation.

        ``.resend_refused`` is there for outer retry wrappers, which typically
        key on ``http_status`` and never look at ``__cause__``; re-entering this
        method mints a fresh key, so retrying a refused resend bills twice.

        A ``deadline_exceeded`` ``504`` normally names a ``Retry-After`` and the
        advice is to resend under the same key. That advice does not survive an
        ``IdempotencyKeyReuse`` on ``__cause__``: this loop already followed it
        and the server refused the key. Treat that pairing as "stop resending" —
        the generation is unreachable under this key — rather than waiting out
        ``retry_after`` for a resend that can only be refused again.

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

        **Resend the alt-provider controls with it.** A key's identity covers the
        query as well as the method and body, and ``model_provider`` /
        ``strict_mode`` / ``fallback_provider`` are query parameters — so a
        recovery call that drops them presents the same key under a DIFFERENT
        query and is refused, leaving the very generation it was meant to collect
        uncollectable. This bites at the server default too: ``strict_mode=False``
        is sent as ``strict_mode=false``, which is a different query from omitting
        it. Pass the call back exactly as it was made::

            client.models.run(
                model, arguments,
                idempotency_key=exc.idempotency_key,
                model_provider=model_provider,
                strict_mode=strict_mode,
                fallback_provider=fallback_provider,
            )

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
        # The first retryable failure, kept so a key refusal on a later attempt
        # cannot bury it. Only the first: every later one is the same call
        # failing again, and the one the caller has to diagnose is the one that
        # started the retrying.
        claimed: BaseException | None = None
        # The key is stamped onto whatever this raises: it is a local of this
        # frame, so an exception that propagates past it would otherwise take
        # the caller's only route back to an already-billed generation with it.
        with translating(idempotency_key=key):
            while True:
                try:
                    return low.post_model_run(
                        model,
                        payload,
                        idempotency_key=key,
                        model_provider=model_provider,
                        strict_mode=strict_mode,
                        fallback_provider=fallback_provider,
                        timeout=timeout,
                    )
                except _CANDIDATE_FAILURES as exc:
                    if claimed is not None and _is_key_reuse(exc):
                        # The resend could never have succeeded — the server
                        # refused the key, not the request — so raising it
                        # would replace the real failure with an artefact of
                        # this loop. Chained, not discarded: the 422 stays
                        # reachable on `__cause__` and in the traceback.
                        raise _resend_refused(_as_sdk_error(claimed, key)) from _as_sdk_error(
                            exc, key
                        )
                    delay = retrier.delay_before_retry(exc)
                    if delay is None:
                        raise
                    if may_have_claimed_key(exc):
                        claimed = exc
                    time.sleep(delay)

    # -- the queued form: submit, hold a handle, collect ------------------
    def run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        strict_mode: bool | None = None,
        fallback_provider: bool | str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any] | BinaryResult:
        """Run ``model`` with ``arguments`` and return the partner's native output.

        See :meth:`_run` for the full contract; this is that call, answering the
        result document alone.

        The result is a ``dict`` for a model whose partner answers JSON and a
        :class:`~comfy_sdk.BinaryResult` for one whose partner answers a
        generation directly as bytes, branched on the response ``Content-Type``
        exactly as the run route's published ``200`` says a client must.

        ``run`` answers the native output because that document is what a caller
        asked for; ``run_detailed`` answers a :class:`RouterRunResult`, which
        carries that same output plus what Router disclosed about HOW the call
        ran. The split exists because an alt-provider response is translated
        back to this model's native contract, so the body alone cannot tell an
        alt-provider run from a native one -- only the headers can, and most
        callers should not pay an envelope for them.
        """
        return self._run(
            model,
            arguments,
            idempotency_key=idempotency_key,
            model_provider=model_provider,
            strict_mode=strict_mode,
            fallback_provider=fallback_provider,
            timeout=timeout,
        )[0]

    def run_detailed(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        strict_mode: bool | None = None,
        fallback_provider: bool | str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> RouterRunResult:
        """:meth:`run`, plus what Router disclosed about how the call ran.

        ``run`` answers the native output because that document is what a caller
        asked for; ``run_detailed`` answers a :class:`RouterRunResult`, which
        carries that same output plus what Router disclosed about HOW the call
        ran. The split exists because an alt-provider response is translated
        back to this model's native contract, so the body alone cannot tell an
        alt-provider run from a native one -- only the headers can, and most
        callers should not pay an envelope for them.
        """
        body, headers = self._run(
            model,
            arguments,
            idempotency_key=idempotency_key,
            model_provider=model_provider,
            strict_mode=strict_mode,
            fallback_provider=fallback_provider,
            timeout=timeout,
        )
        return _run_result(body, headers)

    def submit(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> RequestHandle:
        """Queue ``model`` with ``arguments`` and return a handle to the request.

        The queued counterpart of :meth:`run`, and the same request either way:
        ``model`` is the canonical ``{provider}/{model}`` id and ``arguments``
        is the partner model's own native JSON input, forwarded unchanged. The
        difference is when the server answers — here, as soon as the request is
        *accepted*, with the generation collected later through the returned
        :class:`~comfy_sdk.model_requests.RequestHandle`.

        Reach for it over :meth:`run` when the caller cannot hold a connection
        for the length of a generation: a web request that has to return now, a
        worker that submits in one process and collects in another, or a batch
        where the submits should all be in flight at once.

        **A fresh** ``Idempotency-Key`` **is minted per call**, which is what
        makes two deliberate submits of the same input two requests rather than
        one deduplicated request, while a transport-level retry *inside* this
        one call keeps the one key and so replays the original rather than
        queueing a second generation. Pass ``idempotency_key`` to choose the
        key yourself — the case that earns it is a lost response: the request
        may have been accepted and its id lost with the response, and resending
        under the same key is the only way back to it. The uniqueness rules are
        :meth:`run`'s, unchanged: the keyspace is the whole workspace's, so
        mint keys with real entropy and never from a guessable label.

        Every exception this raises carries that key on ``.idempotency_key``,
        for exactly that recovery.

        The surface is gated server side: a caller the queue is not switched on
        for is answered ``403`` ``not_enabled``, which arrives here as
        :class:`~comfy_sdk.router_exceptions.NotEnabled`. Nothing about the
        request is wrong in that case, and it is terminal — do not retry it.
        """
        low = cast(ComfyLow, self._low)
        key = (
            validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else new_idempotency_key()
        )
        # Snapshotted deeply before the first attempt, for the reason `run`
        # gives: a mutation between attempts would send a different body under
        # the one key, which is the same-key-different-body case the contract
        # refuses outright.
        payload = deepcopy(dict(arguments))
        retrier = Retrier(self._retry, now=_now)
        with translating(idempotency_key=key):
            while True:
                try:
                    body, _headers = low.post_model_submit(model, payload, idempotency_key=key)
                    break
                except _CANDIDATE_FAILURES as exc:
                    delay = retrier.delay_before_retry(exc)
                    if delay is None:
                        raise
                    time.sleep(delay)
            request_id = _request_id_of(body)
        return RequestHandle(low, model, request_id, self._retry)

    def subscribe(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        on_queue_update: Callable[[QueueUpdate], Any] | None = None,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Queue a request, follow it to completion, and return its result.

        :meth:`submit` plus polling plus
        :meth:`~comfy_sdk.model_requests.RequestHandle.get`, in one call — the
        ergonomic form for a caller who does want to wait but also wants to
        show progress while waiting. The return value is the provider's own
        payload, identical to what :meth:`run` would have returned.

        ``on_queue_update`` is called with a
        :class:`~comfy_sdk.model_requests.QueueUpdate` each time the queue
        moves — first observation, every change of status or position, and the
        completion. It is called from this thread, so keep it quick; an
        exception it raises propagates and abandons the wait (the request keeps
        running server side).

        ``timeout`` is a **client-side** bound in seconds on the whole call —
        the submit's wait excluded only where its own retry policy is already
        running, then every poll, retry and pause, and the result fetch — with
        no server-side meaning: the queue's own timeouts are the server's.
        When it runs out this makes a best-effort
        :meth:`~comfy_sdk.model_requests.RequestHandle.cancel` — so a caller
        that has stopped waiting is not also still paying for a generation
        nobody will collect — and then raises ``TimeoutError``. Best-effort is
        literal: a cancel that itself fails is swallowed, because the timeout
        is the failure worth reporting and a masked one would send the caller
        looking in the wrong place. Use :meth:`submit` instead when the request
        should outlive the caller's patience.

        A completion carrying an ``error_type`` — which is how the server
        reports a failure *and* a cancellation — raises the typed router
        exception rather than returning, so a ``200`` never comes back as a
        successful result.
        """
        # The clock starts here, before the submit, so ``timeout`` bounds the
        # whole call as documented and not only the polling after it.
        deadline = None if timeout is None else _now() + timeout
        handle = self.submit(model, arguments, idempotency_key=idempotency_key)
        # ``closing`` so the poll generator is finalised on every exit — the
        # completion, the timeout, and above all the one where the caller's
        # callback raises, which otherwise leaves it suspended until the
        # collector happens to reach it.
        with contextlib.closing(handle.iter_events(timeout=_remaining(deadline))) as updates:
            completion: QueueUpdate | None = None
            while True:
                # Only the *iteration* is inside the ``except TimeoutError``. A
                # callback is the caller's own code and may raise a
                # ``TimeoutError`` of its own — from an HTTP call it makes to
                # render progress, say — and catching that here would cancel a
                # perfectly healthy request on the strength of a failure that
                # had nothing to do with the wait.
                try:
                    update = next(updates)
                except StopIteration:
                    break
                except TimeoutError:
                    try:
                        handle._cancel_best_effort()
                    except _CANCEL_FAILURES:
                        # Best-effort is literal: the timeout is the failure
                        # worth reporting, and a masked one sends the caller
                        # looking in the wrong place.
                        pass
                    raise
                completion = update
                if on_queue_update is not None:
                    on_queue_update(update)
        return handle._collect(_completed(completion), budget=_remaining(deadline))

    def handle(self, model: str, request_id: str) -> RequestHandle:
        """Rebuild the handle for a request submitted anywhere.

        Takes no state beyond the two ids that address the request, so a
        process that never made the submit — a worker draining a queue of ids,
        a retry after a restart — reaches the same
        :class:`~comfy_sdk.model_requests.RequestHandle` the submitting process
        held. Makes no request of its own: an id that names nothing surfaces on
        the first :meth:`~comfy_sdk.model_requests.RequestHandle.status` or
        :meth:`~comfy_sdk.model_requests.RequestHandle.get`, as the server's
        own answer rather than as a guess made here.

        Both ids are validated locally — a malformed ``{provider}/{model}`` id
        or a ``request_id`` that is not a single path segment raises
        ``ValueError`` (a non-string raises ``TypeError``) rather than being
        pasted into a URL.
        """
        parse_model_id(model)
        parse_request_id(request_id)
        return RequestHandle(cast(ComfyLow, self._low), model, request_id, self._retry)


class AsyncModels(_ModelsBase):
    """``client.models`` on :class:`~comfy_sdk.client.AsyncComfy` — mirrors :class:`Models`."""

    def __init__(self, low: AsyncComfyLow, retry: RetryPolicy = DEFAULT_RETRY) -> None:
        self._low = low
        self._retry = retry

    async def _run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        strict_mode: bool | None = None,
        fallback_provider: bool | str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> tuple[dict[str, Any] | BinaryResult, Mapping[str, str]]:
        """Awaitable :meth:`Models.run` — same arguments, same result shape.

        This *is* the async form of ``run``: awaiting it on ``AsyncComfy`` is
        the whole difference from the sync client — including the model-id
        rule, the retry policy, the one-key-per-call rule, the failure raised
        when a resend is refused for key reuse, and the ``.idempotency_key``
        every exception it raises carries for the replay.
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
        # The most recent failure that could have claimed the key — see
        # :meth:`Models.run`.
        claimed: BaseException | None = None
        # The key is stamped onto whatever this raises: it is a local of this
        # frame, so an exception that propagates past it would otherwise take
        # the caller's only route back to an already-billed generation with it.
        with translating(idempotency_key=key):
            while True:
                try:
                    return await low.post_model_run(
                        model,
                        payload,
                        idempotency_key=key,
                        model_provider=model_provider,
                        strict_mode=strict_mode,
                        fallback_provider=fallback_provider,
                        timeout=timeout,
                    )
                except _CANDIDATE_FAILURES as exc:
                    if claimed is not None and _is_key_reuse(exc):
                        # The rejected resend replaces nothing — see
                        # :meth:`Models.run`.
                        raise _resend_refused(_as_sdk_error(claimed, key)) from _as_sdk_error(
                            exc, key
                        )
                    delay = retrier.delay_before_retry(exc)
                    if delay is None:
                        raise
                    if may_have_claimed_key(exc):
                        claimed = exc
                    await asyncio.sleep(delay)

    # -- the queued form: submit, hold a handle, collect ------------------
    async def run(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        strict_mode: bool | None = None,
        fallback_provider: bool | str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> dict[str, Any] | BinaryResult:
        """Awaitable :meth:`Models.run` — same ``dict | BinaryResult`` result."""
        body, _ = await self._run(
            model,
            arguments,
            idempotency_key=idempotency_key,
            model_provider=model_provider,
            strict_mode=strict_mode,
            fallback_provider=fallback_provider,
            timeout=timeout,
        )
        return body

    async def run_detailed(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        model_provider: str | None = None,
        strict_mode: bool | None = None,
        fallback_provider: bool | str | None = None,
        timeout: float | httpx.Timeout | None = MODEL_RUN_TIMEOUT,
    ) -> RouterRunResult:
        """Awaitable :meth:`Models.run_detailed` — same arguments, same result shape."""
        body, headers = await self._run(
            model,
            arguments,
            idempotency_key=idempotency_key,
            model_provider=model_provider,
            strict_mode=strict_mode,
            fallback_provider=fallback_provider,
            timeout=timeout,
        )
        return _run_result(body, headers)

    async def submit(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> AsyncRequestHandle:
        """Awaitable :meth:`Models.submit` — same arguments, an async handle.

        Including the fresh-key-per-call rule and the ``.idempotency_key`` every
        exception carries for a lost-response recovery. See :meth:`Models.submit`.
        """
        low = cast(AsyncComfyLow, self._low)
        key = (
            validate_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else new_idempotency_key()
        )
        payload = deepcopy(dict(arguments))
        retrier = Retrier(self._retry, now=_now)
        with translating(idempotency_key=key):
            while True:
                try:
                    body, _headers = await low.post_model_submit(
                        model, payload, idempotency_key=key
                    )
                    break
                except _CANDIDATE_FAILURES as exc:
                    delay = retrier.delay_before_retry(exc)
                    if delay is None:
                        raise
                    await asyncio.sleep(delay)
            request_id = _request_id_of(body)
        return AsyncRequestHandle(low, model, request_id, self._retry)

    async def subscribe(
        self,
        model: str,
        arguments: Mapping[str, Any],
        *,
        on_queue_update: Callable[[QueueUpdate], Any] | None = None,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Awaitable :meth:`Models.subscribe` — same arguments, same result.

        ``on_queue_update`` may be a plain callable or a coroutine function;
        an awaitable it returns is awaited before the next poll, so an async
        callback does not need wrapping. See :meth:`Models.subscribe`.
        """
        deadline = None if timeout is None else _now() + timeout
        handle = await self.submit(model, arguments, idempotency_key=idempotency_key)
        completion: QueueUpdate | None = None
        try:
            # ``aclosing`` for the reason ``Models.subscribe`` uses ``closing``,
            # and a sharper one: an async generator left suspended is finalised
            # by the event loop's own shutdown hook, well after this call
            # returned.
            async with contextlib.aclosing(
                handle.iter_events(timeout=_remaining(deadline))
            ) as updates:
                while True:
                    # Scoped to the iteration alone, for the reason
                    # `Models.subscribe` gives — and it bites harder here, where
                    # a callback that awaits anything under `asyncio.wait_for`
                    # raises `TimeoutError` natively.
                    try:
                        update = await anext(updates)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        try:
                            await handle._cancel_best_effort()
                        except _CANCEL_FAILURES:
                            pass
                        raise
                    completion = update
                    if on_queue_update is not None:
                        outcome = on_queue_update(update)
                        if isinstance(outcome, Awaitable):
                            await outcome
        except asyncio.CancelledError:
            # The task was cancelled from outside while the request is still
            # queued or running. `subscribe` has exposed neither the handle nor
            # its key, so the caller has no way back to a generation that would
            # otherwise keep running — and keep billing — after they stopped
            # waiting for it. One shielded, bounded best-effort cancel, exactly
            # as on the timeout path, then the cancellation proceeds.
            with contextlib.suppress(*_CANCEL_FAILURES, TimeoutError, asyncio.TimeoutError):
                await asyncio.shield(
                    asyncio.wait_for(handle._cancel_best_effort(), _CANCEL_TIMEOUT + 1.0)
                )
            raise
        return await handle._collect(_completed(completion), budget=_remaining(deadline))

    async def handle(self, model: str, request_id: str) -> AsyncRequestHandle:
        """Awaitable :meth:`Models.handle` — rebuild a handle from the two ids.

        Awaited for symmetry with the rest of the async client rather than
        because it does any I/O; it makes no request, exactly as the sync form
        makes none. See :meth:`Models.handle`.
        """
        parse_model_id(model)
        parse_request_id(request_id)
        return AsyncRequestHandle(cast(AsyncComfyLow, self._low), model, request_id, self._retry)


__all__ = ["Models", "AsyncModels", "BinaryResult", "RouterRunResult"]
