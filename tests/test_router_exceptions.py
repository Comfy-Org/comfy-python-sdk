"""The router exception hierarchy, raised and caught from stubbed error responses.

Each case builds the response the router would actually send -- status, headers,
decoded JSON body -- hands it to ``error_from_response``, raises the result, and
catches it by name. That is the contract a caller depends on, so it is the thing
under test rather than the lookup table on its own.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfy_sdk.exceptions import ComfyError
from comfy_sdk.router_exceptions import (
    ERROR_TYPE_HEADER,
    REQUEST_ID_HEADER,
    ROUTER_ERROR_TYPES,
    ROUTER_EXCEPTIONS,
    ClientDisconnected,
    ConcurrencyLimitExceeded,
    ContentPolicyViolation,
    DeadlineExceeded,
    Forbidden,
    InsufficientCredits,
    InternalError,
    InvalidInput,
    ModelNotFound,
    NotEnabled,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    RouterError,
    ServiceUnavailable,
    Unauthorized,
    ValidationErrorDetail,
    error_from_response,
    exception_for,
)

REQUEST_ID = "6f1a1a6e-6a53-4a5f-9d3a-2b3b0a1f9c21"

# (error_type, the status the router returns with it, the class it must raise).
# The statuses are part of the case on purpose: they document the pairing a
# retry policy keys on (502 provider_error vs 504 provider_timeout), and the
# four status collisions the widened set introduced -- 403 forbidden vs
# not_enabled, 429 concurrency_limit_exceeded vs rate_limited, 504
# provider_timeout vs deadline_exceeded, 500 internal_error vs the 503
# service_unavailable it is deliberately NOT merged with.
CASES: list[tuple[str, int, type[RouterError]]] = [
    ("invalid_input", 400, InvalidInput),
    ("content_policy_violation", 400, ContentPolicyViolation),
    ("provider_error", 502, ProviderError),
    ("provider_timeout", 504, ProviderTimeout),
    ("insufficient_credits", 402, InsufficientCredits),
    ("model_not_found", 404, ModelNotFound),
    ("unauthorized", 401, Unauthorized),
    ("forbidden", 403, Forbidden),
    ("concurrency_limit_exceeded", 429, ConcurrencyLimitExceeded),
    ("client_disconnected", 499, ClientDisconnected),
    ("internal_error", 500, InternalError),
    ("deadline_exceeded", 504, DeadlineExceeded),
    ("not_enabled", 403, NotEnabled),
    ("service_unavailable", 503, ServiceUnavailable),
    ("rate_limited", 429, RateLimited),
]

# Deliberately not in the set this SDK version knows: later milestones add them,
# and until then each must arrive as the base class rather than crash a client.
DEFERRED_ERROR_TYPES = ["file_download_error", "cancelled", "queue_timeout"]


def stub_error_response(
    error_type: str | None,
    status: int,
    *,
    detail: Any = "Something went wrong.",
    request_id: str | None = REQUEST_ID,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """A router error response: the two headers plus the request-level body."""
    headers = {}
    if error_type is not None:
        headers[ERROR_TYPE_HEADER] = error_type
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id

    body: dict[str, Any] = {"detail": detail}
    if error_type is not None:
        body["error_type"] = error_type
    return status, headers, body


# -- one class per error_type, raised and caught -----------------------------


@pytest.mark.parametrize(("error_type", "status", "expected"), CASES)
def test_each_error_type_raises_and_catches_its_own_class(
    error_type: str, status: int, expected: type[RouterError]
) -> None:
    status, headers, body = stub_error_response(error_type, status)

    with pytest.raises(expected) as caught:
        raise error_from_response(status, headers, body)

    exc = caught.value
    assert exc.error_type == error_type
    assert exc.http_status == status
    assert exc.detail == "Something went wrong."
    assert str(exc) == "Something went wrong."


@pytest.mark.parametrize(("error_type", "status", "expected"), CASES)
def test_every_class_is_catchable_as_the_router_base(
    error_type: str, status: int, expected: type[RouterError]
) -> None:
    status, headers, body = stub_error_response(error_type, status)

    with pytest.raises(RouterError) as caught:
        raise error_from_response(status, headers, body)

    assert type(caught.value) is expected


def test_the_router_base_is_a_comfy_error() -> None:
    # A caller who wants everything the SDK can raise keeps one except clause.
    assert issubclass(RouterError, ComfyError)


def test_the_closed_set_is_exactly_the_declared_error_types() -> None:
    assert list(ROUTER_ERROR_TYPES) == [error_type for error_type, _, _ in CASES]
    assert len(ROUTER_EXCEPTIONS) == len(ROUTER_ERROR_TYPES)


@pytest.mark.parametrize("error_type", DEFERRED_ERROR_TYPES)
def test_deferred_error_types_are_not_in_the_closed_set(error_type: str) -> None:
    # Adding one is a decision someone makes on purpose, not a constant that
    # quietly widens a set two SDKs generate from.
    assert error_type not in ROUTER_ERROR_TYPES


def test_class_names_are_the_pascal_case_of_the_wire_value() -> None:
    # The naming rule is what keeps this list and the TypeScript SDK's identical
    # without either side maintaining a second table.
    for cls in ROUTER_EXCEPTIONS:
        expected = "".join(part.capitalize() for part in cls.error_type.split("_"))
        assert cls.__name__ == expected


# -- forward compatibility ---------------------------------------------------


@pytest.mark.parametrize("error_type", [*DEFERRED_ERROR_TYPES, "something_invented_later"])
def test_an_unrecognised_error_type_raises_the_base_class(error_type: str) -> None:
    status, headers, body = stub_error_response(error_type, 500)

    with pytest.raises(RouterError) as caught:
        raise error_from_response(status, headers, body)

    # Base class, not a crash, and the raw value survives for the caller to log.
    assert type(caught.value) is RouterError
    assert caught.value.error_type == error_type
    assert caught.value.request_id == REQUEST_ID


def test_exception_for_maps_known_and_unknown_values() -> None:
    assert exception_for("content_policy_violation") is ContentPolicyViolation
    assert exception_for("queue_timeout") is RouterError
    assert exception_for(None) is RouterError


# -- the request id is on every exception ------------------------------------


@pytest.mark.parametrize(("error_type", "status", "expected"), CASES)
def test_the_request_id_is_attached_to_every_exception(
    error_type: str, status: int, expected: type[RouterError]
) -> None:
    status, headers, body = stub_error_response(error_type, status)
    assert error_from_response(status, headers, body).request_id == REQUEST_ID


def test_the_request_id_header_is_matched_case_insensitively() -> None:
    # Header names are case-insensitive on the wire, and an httpx.Headers is not
    # the only mapping this is handed.
    exc = error_from_response(
        403,
        {"x-comfy-request-id": REQUEST_ID, "x-comfy-error-type": "forbidden"},
        {"detail": "No access.", "error_type": "forbidden"},
    )
    assert isinstance(exc, Forbidden)
    assert exc.request_id == REQUEST_ID


def test_a_missing_request_id_is_none_rather_than_an_error() -> None:
    status, headers, body = stub_error_response("forbidden", 403, request_id=None)
    assert error_from_response(status, headers, body).request_id is None


# -- the 422: structured detail[] entries ------------------------------------

VALIDATION_BODY: dict[str, Any] = {
    "detail": [
        {
            "loc": ["body", "image_url"],
            "msg": "image is smaller than the model's minimum",
            "type": "image_too_small",
            "ctx": {"min_width": 512},
            "input": "https://example.com/tiny.png",
        },
        {
            "loc": ["body", "images", 0],
            "msg": "field required",
            "type": "missing",
        },
    ]
}


def test_validation_detail_entries_are_readable_as_data() -> None:
    # The per-field branch a caller writes has to survive: loc, msg, type and
    # ctx are attributes, not substrings of the message.
    exc = error_from_response(
        422,
        {ERROR_TYPE_HEADER: "invalid_input", REQUEST_ID_HEADER: REQUEST_ID},
        VALIDATION_BODY,
    )

    assert isinstance(exc, InvalidInput)
    assert len(exc.errors) == 2

    first, second = exc.errors
    assert first == ValidationErrorDetail(
        loc=("body", "image_url"),
        msg="image is smaller than the model's minimum",
        type="image_too_small",
        ctx={"min_width": 512},
        input="https://example.com/tiny.png",
    )
    assert first.ctx is not None and first.ctx["min_width"] == 512
    assert first.location == "body.image_url"

    assert second.loc == ("body", "images", 0)  # an integer index stays an int
    assert second.type == "missing"
    assert second.ctx is None and second.input is None
    assert second.location == "body.images.0"


def test_the_validation_message_summarises_without_replacing_the_entries() -> None:
    exc = error_from_response(422, {ERROR_TYPE_HEADER: "invalid_input"}, VALIDATION_BODY)

    assert str(exc) == (
        "body.image_url: image is smaller than the model's minimum; body.images.0: field required"
    )
    assert [e.type for e in exc.errors] == ["image_too_small", "missing"]


def test_the_422_bucket_is_read_off_the_header_the_body_does_not_carry_one() -> None:
    # The per-field body has no error_type field at all, so the header is the
    # only place the bucket appears.
    assert "error_type" not in VALIDATION_BODY
    exc = error_from_response(422, {ERROR_TYPE_HEADER: "invalid_input"}, VALIDATION_BODY)
    assert type(exc) is InvalidInput


def test_a_request_level_failure_carries_no_detail_entries() -> None:
    status, headers, body = stub_error_response("forbidden", 403)
    assert error_from_response(status, headers, body).errors == ()


# -- where the bucket is read from -------------------------------------------


def test_the_header_wins_over_the_body() -> None:
    # One value is written to both by the server, so they cannot normally
    # disagree; if they ever do, the header is the one the contract calls
    # authoritative and the only one the 422 has.
    exc = error_from_response(
        400,
        {ERROR_TYPE_HEADER: "content_policy_violation"},
        {"detail": "Refused.", "error_type": "invalid_input"},
    )
    assert type(exc) is ContentPolicyViolation


def test_the_body_is_used_when_the_header_is_absent() -> None:
    exc = error_from_response(
        402, {}, {"detail": "No credits.", "error_type": "insufficient_credits"}
    )
    assert type(exc) is InsufficientCredits
    assert exc.detail == "No credits."


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, Unauthorized),
        (402, InsufficientCredits),
        (403, Forbidden),
        (404, ModelNotFound),
        (429, ConcurrencyLimitExceeded),
        (499, ClientDisconnected),
        (502, ProviderError),
        (504, ProviderTimeout),
    ],
)
def test_a_response_with_no_bucket_falls_back_to_the_status(
    status: int, expected: type[RouterError]
) -> None:
    # A proxy or gateway that rejects the call before it reaches the router
    # answers with a status and none of the router's headers; `except
    # Unauthorized` should still fire for a rejected key.
    exc = error_from_response(status, {}, None)
    assert type(exc) is expected
    assert exc.detail == f"HTTP {status}"


@pytest.mark.parametrize("status", [400, 422, 503])
def test_an_ambiguous_status_with_no_bucket_stays_the_base_class(status: int) -> None:
    # 400 is either invalid_input or content_policy_violation, and those differ
    # in whether a retry can ever succeed -- guessing would tell a caller to
    # retry a deterministic refusal. 422 is pinned to no bucket at all. 503 is
    # the same shape of problem one layer out: an intermediary's bare 503 says
    # nothing about whether the request ever reached the router, and only the
    # router's own `service_unavailable` is the bucket the contract says clears
    # on its own.
    exc = error_from_response(status, {}, None)
    assert type(exc) is RouterError
    assert exc.error_type == ""


# -- the buckets that share a status with an older one -----------------------


def test_a_not_enabled_403_is_not_the_forbidden_403() -> None:
    # The refusal every caller sees until the rollout reaches them. `forbidden`
    # is an entitlement decision about the caller; this is a state of the
    # rollout, and a caller that cannot tell them apart cannot tell "ask for
    # access" from "wait".
    exc = error_from_response(
        403,
        {ERROR_TYPE_HEADER: "not_enabled", REQUEST_ID_HEADER: REQUEST_ID},
        {
            "detail": "Comfy Router is not switched on for this caller yet.",
            "error_type": "not_enabled",
        },
    )
    assert type(exc) is NotEnabled
    assert not isinstance(exc, Forbidden)
    assert exc.detail == "Comfy Router is not switched on for this caller yet."
    assert exc.request_id == REQUEST_ID
    assert exc.http_status == 403


def test_a_header_less_403_is_still_the_forbidden_403() -> None:
    # Adding `not_enabled` must not make the status fallback ambiguous: the
    # router repeats the bucket on the header on every error it sends, so a
    # 403 without one is an intermediary's, and `forbidden` stays the reading.
    exc = error_from_response(403, {}, None)
    assert type(exc) is Forbidden


def test_a_service_unavailable_503_is_its_own_bucket_not_internal_error() -> None:
    exc = error_from_response(
        503,
        {ERROR_TYPE_HEADER: "service_unavailable", REQUEST_ID_HEADER: REQUEST_ID},
        {"detail": "A dependency is briefly unavailable.", "error_type": "service_unavailable"},
    )
    assert type(exc) is ServiceUnavailable
    assert not isinstance(exc, InternalError)
    assert exc.detail == "A dependency is briefly unavailable."
    assert exc.request_id == REQUEST_ID


def test_a_deadline_exceeded_504_is_not_the_provider_timeout_504() -> None:
    # Same status, opposite cause: one says the partner ran out of time, the
    # other says Comfy stopped holding the connection.
    exc = error_from_response(
        504,
        {ERROR_TYPE_HEADER: "deadline_exceeded"},
        {"detail": "Comfy stopped waiting.", "error_type": "deadline_exceeded"},
    )
    assert type(exc) is DeadlineExceeded
    assert type(error_from_response(504, {}, None)) is ProviderTimeout


def test_a_rate_limited_429_is_not_the_concurrency_429() -> None:
    # One clears when the caller's own in-flight call finishes; nothing the
    # caller does drains the other early.
    exc = error_from_response(
        429,
        {ERROR_TYPE_HEADER: "rate_limited"},
        {"detail": "10 requests per minute.", "error_type": "rate_limited"},
    )
    assert type(exc) is RateLimited
    assert type(error_from_response(429, {}, None)) is ConcurrencyLimitExceeded


# -- never crash the client on a malformed response --------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,
        "<html>502 Bad Gateway</html>",
        b"",
        [],
        {},
        {"detail": None},
        {"detail": 42},
        {"detail": []},
        {"detail": ["not an object", None, 7]},
        {"detail": [{}]},
        {"detail": [{"loc": "body", "msg": None, "type": 3, "ctx": "nope"}]},
        {"error_type": 7, "detail": {"nested": "object"}},
    ],
)
def test_a_malformed_body_degrades_instead_of_raising(body: Any) -> None:
    exc = error_from_response(500, {REQUEST_ID_HEADER: REQUEST_ID}, body)
    assert isinstance(exc, RouterError)
    assert exc.request_id == REQUEST_ID
    assert isinstance(exc.detail, str) and exc.detail
    for entry in exc.errors:
        assert isinstance(entry, ValidationErrorDetail)
        assert isinstance(entry.msg, str)
        assert isinstance(entry.type, str)


def test_a_malformed_entry_keeps_the_fields_that_did_arrive() -> None:
    exc = error_from_response(
        422,
        {ERROR_TYPE_HEADER: "invalid_input"},
        {"detail": [{"msg": "field required", "type": "missing", "loc": None}]},
    )
    entry = exc.errors[0]
    assert entry.msg == "field required"
    assert entry.type == "missing"
    assert entry.loc == ()


def test_an_empty_header_value_is_treated_as_absent() -> None:
    exc = error_from_response(403, {ERROR_TYPE_HEADER: "  ", REQUEST_ID_HEADER: " "}, None)
    assert type(exc) is Forbidden  # from the status fallback, not the blank header
    assert exc.request_id is None


def test_no_headers_at_all_still_produces_an_exception() -> None:
    exc = error_from_response(500, None, None)
    assert type(exc) is RouterError
    assert exc.request_id is None
    assert exc.http_status == 500


# -- the workflow-surface classes are a separate hierarchy -------------------


def test_the_shared_names_are_not_the_workflow_surface_classes() -> None:
    # comfy_sdk.Unauthorized is the workflow surface's. The router ones are
    # imported from this module on purpose -- one name cannot be two classes.
    from comfy_sdk import exceptions as workflow_exceptions

    for router_cls, workflow_cls in (
        (Unauthorized, workflow_exceptions.Unauthorized),
        (Forbidden, workflow_exceptions.Forbidden),
        (InsufficientCredits, workflow_exceptions.InsufficientCredits),
    ):
        assert router_cls is not workflow_cls
        assert not issubclass(router_cls, workflow_cls)
        assert issubclass(router_cls, RouterError)
