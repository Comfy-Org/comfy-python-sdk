"""``comfy_sdk.exceptions`` and ``comfy_sdk.router_exceptions`` are one hierarchy.

The two modules deliberately share names -- ``Unauthorized``, ``Forbidden``,
``InsufficientCredits`` mean the same thing on both the workflow surface and the
Router one. What is *not* allowed is for a shared name to be two different
classes, which is what it was: the class ``to_sdk_error`` raised for a Router
refusal descended from ``ComfyError`` but not from ``RouterError``, so

    try:
        client.models.subscribe(model, params)
    except RouterError:
        ...

compiled, type-checked, read as a correct catch-all, and caught nothing. The
handler was dead in the safe-looking direction: the more careful the caller, the
more certain they were to be wrong.

So the assertions here are about the *pair* of modules rather than about either
one. The per-bucket cases drive a real call against the stub server and catch it
with the broad handler, because ``except RouterError`` around a Router call is
the thing the bug was about; the parity test enumerates both modules' exports
and fails on any shared name that is not one object, so they cannot drift apart
again silently.
"""

from __future__ import annotations

import pytest

from comfy_low.errors import ApiError
from comfy_sdk import AsyncComfy, Comfy
from comfy_sdk import exceptions as sdk_exceptions
from comfy_sdk import router_exceptions as router
from comfy_sdk.retry import NO_RETRY
from comfy_sdk.router_exceptions import (
    AlreadyCompleted,
    CancelRefused,
    ComfyError,
    InsufficientCredits,
    ModelNotFound,
    NotEnabled,
    RouterError,
)

MODEL = "fal-ai/flux-pro"
ARGS = {"prompt": "a cat"}

#: One case per bucket in the contract's closed set, plus the two shapes that
#: are not in it: a bucket added to Router after this SDK version was built, and
#: an unknown bucket on a status the reader has no table entry for. Statuses are
#: the ones ``tests/test_router_exceptions.py`` pins for each bucket, so the
#: pairing stated in one place is the pairing exercised here.
REFUSALS: list[tuple[int, str]] = [
    (400, "invalid_input"),
    (400, "content_policy_violation"),
    (502, "provider_error"),
    (504, "provider_timeout"),
    (402, "insufficient_credits"),
    (404, "model_not_found"),
    (401, "unauthorized"),
    (403, "forbidden"),
    (429, "concurrency_limit_exceeded"),
    (499, "client_disconnected"),
    (500, "internal_error"),
    (504, "deadline_exceeded"),
    (403, "not_enabled"),
    (503, "service_unavailable"),
    (429, "rate_limited"),
    (409, "cancelled"),
    (504, "queue_timeout"),
    (404, "request_not_found"),
    (418, "something_invented_later"),
]


# -- the assertion the bug is about ------------------------------------------


@pytest.mark.parametrize(("status", "bucket"), REFUSALS, ids=[b for _, b in REFUSALS])
def test_every_router_refusal_is_caught_by_the_router_base(server, status, bucket) -> None:
    # `submit` rather than `run`: it is the call the report reproduced against,
    # and its refusal arrives in Router's own error shape (a bucket on the
    # header and in the body, no v2 `error.code` anywhere).
    server.state.queue_submit_error = (status, bucket)
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(RouterError) as caught:
            client.models.submit(MODEL, ARGS)
    assert caught.value.error_type == bucket
    assert caught.value.http_status == status


@pytest.mark.parametrize(("status", "bucket"), REFUSALS, ids=[b for _, b in REFUSALS])
async def test_every_async_router_refusal_is_caught_by_the_router_base(
    server, status, bucket
) -> None:
    # The async twin catches with the same clause: the surfaces share
    # `translating()` and therefore share every class it raises, so a parity
    # gap here would mean one of them had grown its own mapping.
    server.state.queue_submit_error = (status, bucket)
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(RouterError) as caught:
            await client.models.submit(MODEL, ARGS)
    assert caught.value.error_type == bucket


@pytest.mark.parametrize(("status", "bucket"), REFUSALS, ids=[b for _, b in REFUSALS])
def test_every_router_refusal_on_the_run_route_is_caught_too(server, status, bucket) -> None:
    # The awaited route, which reaches `to_sdk_error` down a different call
    # path than `submit` does.
    server.state.model_run_router_error_shape = True
    server.state.model_run_error = (status, bucket)
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(RouterError):
            client.models.run(MODEL, ARGS)


@pytest.mark.parametrize(
    ("status", "bucket", "expected"),
    [
        (402, "insufficient_credits", InsufficientCredits),
        (404, "model_not_found", ModelNotFound),
        (403, "not_enabled", NotEnabled),
    ],
    ids=["insufficient_credits", "model_not_found", "not_enabled"],
)
def test_the_named_class_still_catches_its_own_bucket(server, status, bucket, expected) -> None:
    # The broad catch is not bought by collapsing the set: a caller who names
    # the one failure mode still gets it, and gets it as the class the module
    # documents rather than as a base.
    server.state.queue_submit_error = (status, bucket)
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(expected) as caught:
            client.models.submit(MODEL, ARGS)
    assert type(caught.value) is expected


def test_a_subscribe_that_refuses_for_no_credits_is_caught_by_the_router_base(server) -> None:
    # The call the report reproduced against, spelled the way the report spells
    # it: an account with no credits, `subscribe`, and the broad handler.
    server.state.queue_submit_error = (402, "insufficient_credits")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(RouterError) as caught:
            client.models.subscribe(MODEL, ARGS, timeout=30)
    assert type(caught.value) is InsufficientCredits
    assert caught.value.error_type == "insufficient_credits"


async def test_an_async_subscribe_that_refuses_for_no_credits_is_caught_too(server) -> None:
    server.state.queue_submit_error = (402, "insufficient_credits")
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(RouterError):
            await client.models.subscribe(MODEL, ARGS, timeout=30)


def test_either_import_path_catches_a_refusal(server) -> None:
    # The whole point of the merge: the module a caller happened to import
    # from cannot decide whether their handler fires.
    server.state.queue_submit_error = (402, "insufficient_credits")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(sdk_exceptions.InsufficientCredits):
            client.models.submit(MODEL, ARGS)
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(router.InsufficientCredits):
            client.models.submit(MODEL, ARGS)


# -- the two modules are one hierarchy ---------------------------------------


def _exported(module) -> dict[str, object]:
    """The module's public names, from ``__all__`` and from ``dir()`` alike.

    Both, because ``__all__`` is the documented surface and ``dir()`` is what a
    caller actually reaches: a class left out of ``__all__`` is still
    importable, and a collision hiding there is exactly as dead a handler as
    one in the documented set.
    """
    names = set(getattr(module, "__all__", ())) | {
        name for name in dir(module) if not name.startswith("_")
    }
    return {name: getattr(module, name) for name in sorted(names)}


def test_no_name_means_two_different_things_across_the_two_modules() -> None:
    ours = _exported(sdk_exceptions)
    theirs = _exported(router)
    collisions = {
        name: (ours[name], theirs[name])
        for name in ours.keys() & theirs.keys()
        if ours[name] is not theirs[name]
    }
    assert collisions == {}, (
        "these names are exported by both exception modules as DIFFERENT objects, "
        "so `except <name>` catches what is raised only if the caller guessed the "
        f"right import: {sorted(collisions)}"
    )


def test_the_shared_names_are_the_ones_expected() -> None:
    # A guard on the guard above: if the two modules stopped sharing names at
    # all -- one of them emptied, renamed, or no longer importable the way the
    # test reaches it -- the collision check would pass vacuously.
    shared = _exported(sdk_exceptions).keys() & _exported(router).keys()
    assert {"Unauthorized", "Forbidden", "InsufficientCredits", "RouterError"} <= shared


def test_the_shared_buckets_descend_from_the_router_base() -> None:
    # Stated as its own assertion because identity alone would be satisfied by
    # merging onto the *wrong* side: one class per name that still did not
    # descend from `RouterError` would leave `except RouterError` just as dead.
    for name in ("Unauthorized", "Forbidden", "InsufficientCredits"):
        cls = getattr(sdk_exceptions, name)
        assert issubclass(cls, RouterError), name
        assert issubclass(cls, ComfyError), name


# -- a cancel the server declines --------------------------------------------


def test_a_cancel_of_a_finished_request_raises_a_named_exception(server) -> None:
    # `409 {"status": "ALREADY_COMPLETED"}` names no error bucket at all, so it
    # used to arrive as an untyped error whose only distinguishing feature was
    # the text of the body. Nothing here matches on that text.
    server.state.queue_cancel_refusal = (409, {"status": "ALREADY_COMPLETED"})
    with Comfy(retry=NO_RETRY) as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises(AlreadyCompleted) as caught:
            handle.cancel()
    exc = caught.value
    assert exc.http_status == 409
    assert exc.error_type == "already_completed"
    assert isinstance(exc, CancelRefused)
    assert isinstance(exc, RouterError)
    assert isinstance(exc, ComfyError)


async def test_an_async_cancel_of_a_finished_request_raises_the_same_class(server) -> None:
    server.state.queue_cancel_refusal = (409, {"status": "ALREADY_COMPLETED"})
    async with AsyncComfy(retry=NO_RETRY) as client:
        handle = await client.models.submit(MODEL, ARGS)
        with pytest.raises(AlreadyCompleted):
            await handle.cancel()


def test_the_refusal_status_value_is_matched_case_insensitively(server) -> None:
    # The value is an enum-like token, so its case carries no meaning; a
    # deployment that spells it back in another one must not fall through to an
    # untyped 409.
    server.state.queue_cancel_refusal = (409, {"status": "already_completed"})
    with Comfy(retry=NO_RETRY) as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises(AlreadyCompleted):
            handle.cancel()


def test_a_409_that_states_no_status_is_not_typed_as_a_cancel_refusal(server) -> None:
    # The reading is narrow on purpose: a `status` field is not an error code,
    # and a 409 that names neither a bucket nor a known refusal has nothing
    # this SDK can honestly call it.
    server.state.queue_cancel_refusal = (409, {"detail": "nope"})
    with Comfy(retry=NO_RETRY) as client:
        handle = client.models.submit(MODEL, ARGS)
        with pytest.raises(ComfyError) as caught:
            handle.cancel()
    # Exactly `ComfyError`, not merely "not a `CancelRefused`": a `409` that
    # named no bucket is not a Router verdict either, so `RouterError` would
    # pass the weaker assertion while being the wrong answer.
    assert type(caught.value) is ComfyError


def test_an_unrelated_409_body_status_on_another_route_is_left_alone(server) -> None:
    # A `409` naming a real bucket is decided by that bucket, not by this
    # table: the refusal reading is consulted only after both code sources
    # come up empty.
    server.state.queue_submit_error = (409, "concurrency_limit_exceeded")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(router.ConcurrencyLimitExceeded):
            client.models.submit(MODEL, ARGS)


@pytest.mark.parametrize("http_status", [400, 404, 410, 500])
def test_an_already_completed_envelope_code_off_409_is_not_a_cancel_refusal(
    http_status: int,
) -> None:
    """`already_completed` as an envelope ``code`` is not the cancel refusal.

    `_BY_CANCEL_REFUSAL` is keyed on the code, but `code` is also whatever a v2
    envelope's `error.code` said -- on any route, at any status. The cancel
    refusal is specifically the CANCEL route answering `409`, and
    `AlreadyCompleted` is documented as benign ("the result is still
    collectable"). Typing some other route's failure that way would make that
    promise on a response that never offered it, so the lookup is gated on the
    status the refusal actually arrives with.
    """
    exc = ApiError(
        "already completed",
        code="already_completed",
        http_status=http_status,
    )
    assert sdk_exceptions._class_for(exc) is not AlreadyCompleted
    assert not issubclass(sdk_exceptions._class_for(exc), CancelRefused)


def test_an_already_completed_envelope_code_on_409_still_maps() -> None:
    # The gate narrows the lookup; it does not remove it. The status the
    # refusal really arrives with still reaches `AlreadyCompleted`, so this
    # pins the boundary from the other side.
    exc = ApiError("already completed", code="already_completed", http_status=409)
    assert sdk_exceptions._class_for(exc) is AlreadyCompleted
