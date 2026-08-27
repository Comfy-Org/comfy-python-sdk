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
# `POST /api/v2/models/run` is fronted by Router, whose error body is
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


def test_a_documented_status_code_is_not_retyped_by_the_bucket() -> None:
    # The guard on the fallback's placement. Router's 429 bucket is
    # `rate_limited` / `concurrency_limit_exceeded`, but this API's 429 has
    # meant `queue_full` -- and `QueueFull` -- since before Router fronted
    # anything. Letting the bucket win here would silently retype an exception
    # integrators already catch, so the status-derived code is consulted first
    # and only the 5xx range, where the status determines nothing, is left to
    # the bucket.
    err = error_from_envelope(429, None, error_type="concurrency_limit_exceeded", retry_after=3)
    assert err.code == "queue_full"
    assert isinstance(err, QueueFull)
    # And the pace survives, which is what `comfy_sdk.retry` keys the 429 on.
    assert err.retry_after == 3


def test_a_router_validation_body_degrades_rather_than_coercing_its_detail() -> None:
    # Router's per-field validation body is the fal/FastAPI `detail[]` shape. A
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
