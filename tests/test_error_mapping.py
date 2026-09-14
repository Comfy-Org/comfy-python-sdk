"""How an error response on the wire becomes a typed exception.

`to_sdk_error` mapping the server's 404 codes to the typed `NotFound`, and
`error_from_envelope` reading the two body shapes this API answers in.
"""

from __future__ import annotations

import httpx
import pytest

import comfy_low.transport as low_transport
from comfy_low.errors import (
    ApiError,
    QueueFull,
    Unauthorized,
    clean_body_excerpt,
    error_from_envelope,
)
from comfy_sdk.exceptions import ComfyError, NotFound, to_sdk_error
from comfy_sdk.exceptions import Unauthorized as SdkUnauthorized
from comfy_sdk.retry import RetryPolicy, error_bucket_of, is_collectable


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


# --- a response nothing in the stack recognised ---
#
# A 5xx answered by a load balancer in front of the deployment carries no
# envelope, no Router bucket and no `X-Comfy-Request-Id` -- just a status and a
# line of plain text naming the cause. Both halves of that used to be lost: the
# code degraded to `"error"`, which is the class default an exception built by
# hand carries and so says nothing about the response, and the text was
# discarded with the response, leaving nothing in any log to diagnose from.


def test_an_unrecognised_status_becomes_a_code_naming_that_status() -> None:
    err = error_from_envelope(503, None)
    assert err.code == "http_503"
    assert type(err) is ApiError


def test_a_status_the_table_names_is_untouched_by_that_fallback() -> None:
    # The fallback is reached only after `_CODE_BY_STATUS`, so a bare 401 with
    # no body at all still maps to the typed Unauthorized a caller catches.
    err = error_from_envelope(401, None)
    assert isinstance(err, Unauthorized)
    assert err.code == "unauthorized"


def test_an_envelope_code_still_wins_over_the_status_fallback() -> None:
    err = error_from_envelope(503, {"error": {"code": "queue_full", "message": "full"}})
    assert isinstance(err, QueueFull)
    assert err.code == "queue_full"
    assert err.body_excerpt is None


def test_the_body_excerpt_is_kept_and_shown_beside_a_bare_status() -> None:
    err = error_from_envelope(503, None, body_excerpt=clean_body_excerpt("no healthy upstream"))
    assert err.body_excerpt == "no healthy upstream"
    # The one string a caller prints, and a logger formats, has to carry the
    # cause -- reading `.body_excerpt` is the deliberate second step, not the
    # only one.
    assert str(err) == "HTTP 503: no healthy upstream"


def test_a_message_the_server_actually_sent_is_not_joined_to_the_excerpt() -> None:
    # The excerpt exists because a response with no message states its cause
    # nowhere else. Where the response did state one, a second copy of the body
    # would be noise -- so the excerpt is dropped, not merely hidden, and
    # `.body_excerpt` being set always means the message is a synthetic one.
    err = error_from_envelope(
        503, {"detail": "router is draining"}, body_excerpt='{"detail": "router is draining"}'
    )
    assert str(err) == "router is draining"
    assert err.body_excerpt is None


def test_an_envelope_naming_a_code_but_no_message_keeps_the_excerpt() -> None:
    # The gate is "did the body state a message", not "was the body JSON": a
    # code alone leaves the message synthetic, and the excerpt is then the only
    # text the response contributed.
    body = {"error": {"code": "queue_full"}}
    err = error_from_envelope(429, body, body_excerpt='{"error": {"code": "queue_full"}}')
    assert isinstance(err, QueueFull)
    assert str(err) == 'HTTP 429: {"error": {"code": "queue_full"}}'


def test_an_excerpt_is_collapsed_to_one_line_and_bounded() -> None:
    # An HTML error page is the realistic non-envelope body, and it arrives
    # indented across many lines. Collapsing before bounding spends the budget
    # on words rather than on the margin.
    excerpt = clean_body_excerpt("<html>\n  <body>\t503 Service Unavailable</body>\n</html>")
    assert excerpt == "<html> <body> 503 Service Unavailable</body> </html>"


def test_a_two_kilobyte_body_is_truncated() -> None:
    excerpt = clean_body_excerpt("x" * 2048)
    assert excerpt is not None
    assert len(excerpt) == 256


@pytest.mark.parametrize("raw", ["", "   ", "\n\t ", None, b"bytes", 503])
def test_a_body_that_says_nothing_leaves_no_excerpt(raw: object) -> None:
    assert clean_body_excerpt(raw) is None


def test_a_terminal_escape_in_the_body_is_not_written_into_a_log() -> None:
    # The excerpt is server-controlled text headed for a traceback, so it is
    # reduced to something safe to display -- the same reason the request id is.
    assert clean_body_excerpt("no healthy \x1b[31mupstream\x00") == "no healthy [31mupstream"


def test_a_control_character_between_words_leaves_a_space_not_a_splice() -> None:
    # Deleting the byte would glue the words it separated into one that was
    # never sent; a space keeps the excerpt readable as what the server said.
    assert clean_body_excerpt("no healthy\x00upstream") == "no healthy upstream"


def test_bidi_overrides_and_zero_width_characters_are_removed_too() -> None:
    # A right-to-left override reverses how the rest of a log line reads, and a
    # zero-width space hides a break; neither is a control character in the C0/C1
    # sense, so the reduction is by Unicode category rather than by code point.
    raw = "\ufeffno\u202ehealthy\u200bupstream\U000f0000"
    assert clean_body_excerpt(raw) == "no healthy upstream"


def test_leading_whitespace_beyond_the_examined_window_still_yields_the_words() -> None:
    # Only a bounded head of the body is examined, so the work does not scale
    # with a multi-megabyte error page; leading whitespace is stripped before
    # the window is taken so an indented page still spends it on words.
    assert clean_body_excerpt("\n" * 10_000 + "no healthy upstream") == "no healthy upstream"


# --- what the transport actually passes at the raise site ---


def _raised_by_transport(resp: httpx.Response) -> ApiError:
    """The exception ``parse_or_raise`` builds for ``resp``.

    Driven through the real raise site rather than through
    ``error_from_envelope`` directly: what a caller sees depends on what the
    transport chooses to pass, and that choice is the half a unit test of the
    builder cannot reach.
    """
    prepared = low_transport._Prepared("http://example.invalid", None)
    with pytest.raises(ApiError) as caught:
        prepared.parse_or_raise(resp, (200,))
    return caught.value


def test_the_transport_keeps_the_text_of_a_load_balancers_503() -> None:
    err = _raised_by_transport(httpx.Response(503, text="no healthy upstream"))
    assert err.code == "http_503"
    assert err.body_excerpt == "no healthy upstream"
    assert str(err) == "HTTP 503: no healthy upstream"


def test_the_transport_keeps_no_excerpt_when_the_body_was_an_envelope() -> None:
    err = _raised_by_transport(
        httpx.Response(429, json={"error": {"code": "queue_full", "message": "full"}})
    )
    assert err.code == "queue_full"
    assert err.body_excerpt is None


def test_a_json_body_that_is_not_an_object_still_yields_an_excerpt() -> None:
    # `resp.json()` succeeds and returns a list; nothing in it is an envelope,
    # so the text is still the only statement of the cause.
    err = _raised_by_transport(httpx.Response(500, json=["upstream refused"]))
    assert err.code == "http_500"
    assert err.body_excerpt == '["upstream refused"]'


def test_a_json_object_that_is_not_an_envelope_still_yields_an_excerpt() -> None:
    # An intermediary that answers in JSON but not in the envelope shape --
    # `{"message": ...}` with no `error` and no `detail` -- is a dict, and was
    # treated as an envelope on that basis alone, losing the one line that named
    # the cause. The gate is whether a message was read, not whether the body
    # parsed as an object.
    err = _raised_by_transport(httpx.Response(503, text='{"message": "no healthy upstream"}'))
    assert err.code == "http_503"
    assert err.body_excerpt == '{"message": "no healthy upstream"}'
    assert str(err) == 'HTTP 503: {"message": "no healthy upstream"}'


def test_a_non_string_envelope_message_cannot_break_str() -> None:
    # `str(exc)` is what a logger calls; a list where the envelope promised a
    # string must degrade to the synthetic message, not raise `TypeError:
    # __str__ returned non-string` from inside the log call.
    body = '{"error": {"code": "boom", "message": ["not", "a", "string"]}}'
    err = _raised_by_transport(httpx.Response(500, text=body))
    assert err.code == "boom"
    assert err.message == "HTTP 500"
    assert str(err) == f"HTTP 500: {body}"


def test_an_unread_streaming_body_degrades_instead_of_raising() -> None:
    # `resp.text` raises ResponseNotRead on a streaming response nothing has
    # read yet -- the same case `resp.json()` degrades on. A failure while
    # composing an error message must not replace the error it describes.
    resp = httpx.Response(503, stream=httpx.ByteStream(b"no healthy upstream"))
    err = _raised_by_transport(resp)
    assert err.code == "http_503"
    assert err.body_excerpt is None


# --- the code survives the layer boundary ---


def test_the_status_code_reaches_the_sdk_surface_unchanged() -> None:
    # `to_sdk_error` re-types a preserved Router bucket; `http_<status>` is not
    # one, and must arrive intact rather than being re-typed or flattened. A
    # caller (and the e2e harnesses) read the `http_` prefix as "no service
    # verdict was reached".
    err = to_sdk_error(error_from_envelope(503, None, body_excerpt="no healthy upstream"))
    assert type(err) is ComfyError
    assert err.code == "http_503"
    assert err.http_status == 503
    # The excerpt has to cross the layer boundary too: an integrator only ever
    # holds the SDK exception, and `str(err)` is what their logger formats.
    assert str(err) == "HTTP 503: no healthy upstream"


def test_the_excerpt_reaches_a_typed_sdk_error_as_well() -> None:
    # Not just the plain fallback: a bare 401 with a text body maps to the typed
    # Unauthorized, and its message carries the text the same way.
    err = to_sdk_error(error_from_envelope(401, None, body_excerpt="token revoked"))
    assert isinstance(err, SdkUnauthorized)
    assert str(err) == "HTTP 401: token revoked"


def test_an_unrecognised_status_is_still_not_retried_on_its_own() -> None:
    # The default policy retries a completed 5xx only where the contract says
    # the Idempotency-Key survives it. `http_503` names no such contract, so it
    # stays exactly as unretryable as the `"error"` it replaces.
    exc = error_from_envelope(503, None)
    assert error_bucket_of(exc) == "http_503"
    assert is_collectable(exc) is False
    assert RetryPolicy().should_retry(exc) is False


def test_an_undecodable_success_body_keeps_what_was_served_instead() -> None:
    # The sibling raise site: a proxy interstitial served as a 200. The message
    # says the body would not decode; only the excerpt says what answered.
    err = _raised_by_transport(httpx.Response(200, text="<html>proxy: gateway timeout</html>"))
    assert err.code == "invalid_response"
    assert err.body_excerpt == "<html>proxy: gateway timeout</html>"
    assert str(err) == (
        "Could not decode the 200 response body as JSON: <html>proxy: gateway timeout</html>"
    )
