"""How an error response on the wire becomes a typed exception.

`to_sdk_error` mapping the server's 404 codes to the typed `NotFound`, and
`error_from_envelope` reading the two body shapes this API answers in.
"""

from __future__ import annotations

import pytest

from comfy_low.errors import ApiError, QueueFull, error_from_envelope
from comfy_sdk.exceptions import NotFound, to_sdk_error


@pytest.mark.parametrize("code", ["not_found", "job_not_found", "asset_not_found"])
def test_404_codes_map_to_notfound(code: str) -> None:
    # public-api returns entity-specific 404 codes (job_not_found / asset_not_found)
    # alongside the spec's generic not_found; all must raise the typed NotFound.
    err = to_sdk_error(ApiError("not found", code=code, http_status=404))
    assert isinstance(err, NotFound)
    assert err.code == code


# --- the two error shapes one client has to read ---
#
# `POST /v2/models/{provider}/{model}` is Router's own route, whose error body is
# `{detail, error_type}` with the coarse bucket repeated on `X-Comfy-Error-Type`
# -- not the v2 `{error: {code, message}}` envelope every other route answers in.
# `error_from_envelope` has to read both, because `comfy_sdk.retry` keys its
# default-on collect rule on that bucket and would otherwise be a silent no-op
# against every real Router 504.


def test_the_v2_envelope_code_still_wins() -> None:
    err = error_from_envelope(
        504,
        {"error": {"code": "generation_in_progress", "message": "still running"}},
        error_type="deadline_exceeded",
    )
    assert err.code == "generation_in_progress"
    assert err.message == "still running"


def test_routers_body_error_type_is_read_as_the_code() -> None:
    err = error_from_envelope(
        504, {"detail": "upstream is slow", "error_type": "deadline_exceeded"}
    )
    assert err.code == "deadline_exceeded"
    assert err.message == "upstream is slow"


def test_routers_error_type_header_is_read_when_the_body_carries_none() -> None:
    # The header is the bucket's other home, and `spec/router-openapi.yaml`
    # marks it required on every Router error response.
    err = error_from_envelope(504, None, error_type="deadline_exceeded")
    assert err.code == "deadline_exceeded"
    # A body-less error response is still diagnosable by status.
    assert err.message == "HTTP 504"


@pytest.mark.parametrize(
    ("status", "bucket"),
    [
        (403, "not_enabled"),
        (404, "model_not_found"),
        (409, "invalid_input"),
        (422, "invalid_input"),
        (429, "rate_limited"),
        (429, "concurrency_limit_exceeded"),
        (500, "provider_error"),
    ],
)
def test_a_router_bucket_outranks_the_status_table_on_every_status(
    status: int, bucket: str
) -> None:
    # The status table exists for responses that carry no bucket at all. A
    # response that names one is Router speaking for itself, and letting the
    # table win destroyed the bucket three ways on live traffic: 422
    # `invalid_input` surfaced as `invalid_workflow`, 409s as `hash_mismatch`
    # (killing the collect rule), and 403 `not_enabled` as `forbidden` -- so
    # `except NotEnabled`, the one handler every pre-launch caller writes,
    # never fired.
    err = error_from_envelope(status, None, error_type=bucket, retry_after=3)
    assert err.code == bucket
    # The pace survives regardless of the code: `comfy_sdk.retry` keys the
    # 429 branch on status + Retry-After, not on `queue_full`.
    assert err.retry_after == 3


def test_a_bucketless_429_still_means_queue_full() -> None:
    # The workflow surface's own 429 carries no bucket; the status table is
    # still what names it, exactly as before the generalization.
    err = error_from_envelope(429, None, retry_after=3)
    assert err.code == "queue_full"
    assert isinstance(err, QueueFull)
    assert err.retry_after == 3


def test_a_router_validation_body_degrades_rather_than_coercing_its_detail() -> None:
    # Router's per-field validation body is the FastAPI `detail[]` shape. A
    # list is not a message: stringifying it would put a Python repr in front of
    # a caller, so the status-derived message answers instead.
    err = error_from_envelope(
        500,
        {"detail": [{"loc": ["body", "steps"], "msg": "too large", "type": "value_error"}]},
        error_type="internal_error",
    )
    assert err.code == "internal_error"
    assert err.message == "HTTP 500"


@pytest.mark.parametrize("body", [None, {}, {"error": None}, {"error_type": "   "}, {"detail": 7}])
def test_a_body_that_names_no_bucket_still_falls_back_to_the_status(body: object) -> None:
    # Degrading to the status-derived code is what keeps a malformed error
    # response diagnosable rather than replacing it with a decoding failure.
    err = error_from_envelope(401, body)  # type: ignore[arg-type]
    assert err.code == "unauthorized"


# --- a preserved bucket becoming the typed RouterError ---
#
# Keeping the bucket as the `code` is only half the fix: `models.run` raises
# through `to_sdk_error`, so the bucket must also select the RouterError
# subclass there or `except NotEnabled` still catches nothing.


@pytest.mark.parametrize(
    ("bucket", "status", "cls_name"),
    [
        ("not_enabled", 403, "NotEnabled"),
        ("invalid_input", 422, "InvalidInput"),
        ("model_not_found", 404, "ModelNotFound"),
        ("concurrency_limit_exceeded", 409, "ConcurrencyLimitExceeded"),
        ("rate_limited", 429, "RateLimited"),
        ("provider_error", 500, "ProviderError"),
        ("deadline_exceeded", 504, "DeadlineExceeded"),
    ],
)
def test_a_router_only_bucket_raises_its_typed_class(
    bucket: str, status: int, cls_name: str
) -> None:
    import comfy_sdk.router_exceptions as rx

    err = to_sdk_error(ApiError("router said no", code=bucket, http_status=status, retry_after=7))
    assert type(err) is getattr(rx, cls_name)
    assert err.code == bucket
    assert err.http_status == status
    assert err.retry_after == 7


@pytest.mark.parametrize("code", ["unauthorized", "forbidden", "insufficient_credits"])
def test_a_bucket_both_surfaces_spell_keeps_its_v2_class(code: str) -> None:
    # These three codes are spelled identically by the v2 envelope and by
    # Router, and the code string alone cannot say which surface answered.
    # Retyping them to the RouterError twins would break every jobs-surface
    # handler to fix none -- the v2 classes fire on the router surface too.
    import comfy_sdk.exceptions as sdk

    err = to_sdk_error(ApiError("no", code=code, http_status=403))
    assert type(err) is getattr(sdk, "".join(p.title() for p in code.split("_")))
