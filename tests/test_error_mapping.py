"""How an error response on the wire becomes a typed exception.

`to_sdk_error` mapping the server's 404 codes to the typed `NotFound`,
`error_from_envelope` reading the two body shapes this API answers in, and
which statuses may be decoded to a typed code when the response named none.
"""

from __future__ import annotations

import httpx
import pytest

import comfy_low.transport as low_transport
from comfy_low.errors import (
    ApiError,
    HashMismatch,
    QueueFull,
    Unauthorized,
    clean_body_excerpt,
    error_from_envelope,
)
from comfy_sdk.exceptions import ComfyError, NotFound, to_sdk_error
from comfy_sdk.exceptions import HashMismatch as SdkHashMismatch
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


# --- which statuses the status table may decode, and which it may not ---
#
# The table is consulted only for a response that named no code of its own, so
# it never sees the compliant envelope surface -- it sees Router-shaped bodies
# and intermediaries, which can answer a status for anything. A typed guess is
# therefore admissible only when every meaning the contract gives a status asks
# the caller for the SAME action. 409 fails that: the contract spells it both
# `hash_mismatch` (POST /assets) -- re-upload the bytes -- and `asset_in_use`
# (DELETE /assets/{id}), which is not a bytes problem at all.


def test_a_bucketless_409_is_not_guessed_to_be_a_hash_mismatch() -> None:
    err = error_from_envelope(409, None)
    assert type(err) is ApiError
    assert not isinstance(err, HashMismatch)
    # `http_409` is #141's name for "no verdict was reached", not a code the
    # contract defines -- the point is that it is not `hash_mismatch`.
    assert err.code == "http_409"
    assert err.http_status == 409
    # `HashMismatch` tells the caller to re-upload bytes, which is why this
    # status cannot be guessed: it is a distinct action, not a vaguer wording
    # of the same one.
    sdk_err = to_sdk_error(err)
    assert type(sdk_err) is ComfyError
    assert not isinstance(sdk_err, SdkHashMismatch)
    assert sdk_err.http_status == 409


def test_a_bucketless_409_keeps_the_message_the_server_sent() -> None:
    # Dropping the guessed code must not drop what the server actually said:
    # the reason for the conflict is the only thing left that explains it, so
    # it has to survive to the surface the caller reads.
    err = error_from_envelope(409, {"error": {"message": "asset is still referenced"}})
    assert err.code == "http_409"
    assert err.http_status == 409
    assert err.message == "asset is still referenced"
    assert to_sdk_error(err).message == "asset is still referenced"


def test_an_empty_envelope_code_reads_as_absent_not_as_a_code() -> None:
    # `""` is not None, so without `_clean` it would short-circuit both the
    # Router bucket and the status table and survive as the code itself --
    # turning a bare 401 into `ApiError(code="")` instead of `Unauthorized`.
    for blank in ("", "   "):
        err = error_from_envelope(401, {"error": {"code": blank}})
        assert err.code == "unauthorized", blank
        assert isinstance(err, Unauthorized), blank
        assert type(to_sdk_error(err)) is SdkUnauthorized, blank


def test_a_bucketless_409_still_carries_the_pace_the_server_named() -> None:
    # Dropping the code must not drop the header: a conflict that named a
    # `Retry-After` is still telling the caller when to ask again.
    err = error_from_envelope(409, None, retry_after=7)
    assert err.retry_after == 7
    assert to_sdk_error(err).retry_after == 7


def test_an_enveloped_409_is_still_a_hash_mismatch() -> None:
    # The assets path is unaffected whenever the body decodes: a real hash
    # mismatch names itself, and `error.code` wins outright.
    err = error_from_envelope(409, {"error": {"code": "hash_mismatch", "message": "m"}})
    assert type(err) is HashMismatch
    assert err.code == "hash_mismatch"
    assert type(to_sdk_error(err)) is SdkHashMismatch


def test_a_bodyless_401_still_maps_to_unauthorized() -> None:
    # Only 409 was dropped -- the rest of the table decodes exactly as before.
    err = error_from_envelope(401, None)
    assert err.code == "unauthorized"
    assert isinstance(err, Unauthorized)


def test_a_router_validation_body_is_read_rather_than_coerced_into_the_message() -> None:
    # Router's per-field validation body is the FastAPI `detail[]` shape. A
    # list is still not a message -- stringifying it would put a Python repr in
    # front of a caller -- so the entries are read instead: they ride up raw for
    # the translation boundary to type, and their own `msg` values become the
    # message.
    err = error_from_envelope(
        500,
        {"detail": [{"loc": ["body", "steps"], "msg": "too large", "type": "value_error"}]},
        error_type="internal_error",
    )
    assert err.code == "internal_error"
    # `<loc>: <msg>`, the same rendering the queued surface uses.
    assert err.message == "body.steps: too large"
    # The intent the old status-derived message protected, unchanged: whatever
    # reaches `str(exc)` is prose, never a repr of the array.
    assert "[" not in err.message
    assert err.validation_errors == (
        {"loc": ["body", "steps"], "msg": "too large", "type": "value_error"},
    )


def test_a_validation_body_whose_entries_name_nothing_still_degrades() -> None:
    # The entries decoded, so they are carried; none of them named a field OR a
    # reason, so there is nothing to say but the status. Both halves matter: a
    # caller that branches on `.errors` still gets them, and the message never
    # becomes an empty string. (An entry that names a field but no message is a
    # separate case -- it surfaces the field, see the loc-only test below.)
    err = error_from_envelope(
        422,
        {"detail": [{"type": "missing"}, {"msg": "   "}]},
        error_type="invalid_input",
    )
    assert err.message == "HTTP 422"
    assert err.validation_errors == ({"type": "missing"}, {"msg": "   "})


def test_a_validation_entry_with_a_loc_but_no_msg_names_the_field() -> None:
    # A field named with no reason still beats the bare status: the loc alone
    # tells the caller which field the server rejected. A blank `msg` reads as
    # absent, exactly as `_clean` treats every other whitespace-only wire string.
    err = error_from_envelope(
        422,
        {"detail": [{"loc": ["body", "steps"]}, {"msg": "   "}]},
        error_type="invalid_input",
    )
    assert err.message == "body.steps"
    assert err.validation_errors == ({"loc": ["body", "steps"]}, {"msg": "   "})


def test_a_string_detail_still_wins_over_the_array_reading() -> None:
    # The request-level shape is untouched: `detail` as a string is the message
    # and carries no entries.
    err = error_from_envelope(403, {"detail": "not enabled"}, error_type="not_enabled")
    assert err.message == "not enabled"
    assert err.validation_errors == ()


def test_an_envelope_message_outranks_the_validation_entries() -> None:
    # Precedence is unchanged by the new reading: `error.message` is the
    # envelope's own statement of the cause and still wins. The entries are
    # carried regardless -- they are data, not a fallback for the message.
    err = error_from_envelope(
        422,
        {
            "error": {"code": "invalid_workflow", "message": "the graph is invalid"},
            "detail": [{"loc": ["body", "steps"], "msg": "too large"}],
        },
    )
    assert err.message == "the graph is invalid"
    assert err.validation_errors == ({"loc": ["body", "steps"], "msg": "too large"},)


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


def test_a_validation_array_that_states_a_message_drops_the_excerpt_too() -> None:
    # The same rule reached through the array: the entries stated the cause, so
    # the raw JSON stops being glued onto `str(exc)`. That gluing was the whole
    # of what a caller used to see for a per-field failure.
    raw = '{"detail": [{"loc": ["body", "steps"], "msg": "too large"}]}'
    err = error_from_envelope(
        422,
        {"detail": [{"loc": ["body", "steps"], "msg": "too large"}]},
        error_type="invalid_input",
        body_excerpt=raw,
    )
    assert str(err) == "body.steps: too large"
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


# --- the per-field summary is sanitised, bounded, and one string per body ---
#
# The `detail[]` summary reaches `str(exc)`, a traceback and a log line, and its
# `msg` is server-, proxy- or provider-controlled text. It gets the same
# treatment every other body-derived string gets (`clean_body_excerpt`), and the
# awaited `models.run` path and the queued surface render it through one
# summariser so a caller's branch cannot work on one and silently not the other.


def test_summarise_detail_names_each_field_rather_than_collapsing() -> None:
    # Two `field required` entries must not collapse to the unrecoverable
    # `field required; field required`: the loc is what tells them apart.
    from comfy_low.errors import summarise_detail

    summary = summarise_detail(
        [
            {"loc": ["body", "a"], "msg": "field required"},
            {"loc": ["body", "b"], "msg": "field required"},
        ]
    )
    assert summary == "body.a: field required; body.b: field required"
    # An integer index renders like the queued surface's `.location`.
    assert summarise_detail([{"loc": ["body", "images", 0], "msg": "bad"}]) == "body.images.0: bad"
    # Nothing to summarise -> None, so the caller falls back to the status.
    assert summarise_detail([]) is None
    assert summarise_detail([1, "x"]) is None
    assert summarise_detail("not a sequence of entries") is None


def test_a_validation_summary_is_sanitised_before_it_reaches_str() -> None:
    # A newline, a C0 NUL and an ANSI escape in a provider `msg` must not reach
    # the terminal reading the error verbatim -- the escape's ESC byte is the
    # dangerous part and is reduced to a space, whitespace collapses to one line.
    err = error_from_envelope(
        422,
        {"detail": [{"loc": ["body", "steps"], "msg": "line one\nline\x1b[2Jtwo\x00three"}]},
        error_type="invalid_input",
    )
    assert "\n" not in err.message
    assert "\x1b" not in err.message
    assert "\x00" not in err.message
    assert err.message == "body.steps: line one line [2Jtwo three"


def test_a_validation_summary_is_capped_at_the_excerpt_limit() -> None:
    # Many long entries could otherwise make the message arbitrarily large; the
    # same 256-char cap the excerpt gets applies to the summary.
    from comfy_low.errors import _BODY_EXCERPT_LIMIT

    err = error_from_envelope(
        422,
        {"detail": [{"msg": "x" * 5000} for _ in range(10)]},
        error_type="invalid_input",
    )
    assert len(err.message) == _BODY_EXCERPT_LIMIT


def test_a_detail_array_of_non_mappings_never_leaks_a_list_repr() -> None:
    # When the array yields no summary -- every member a non-mapping -- the
    # message falls through to `_clean(raw_detail)` with the LIST. `_clean`'s
    # isinstance guard is load-bearing there: without it the caller's message
    # would be a Python list repr. It degrades to the status instead.
    err = error_from_envelope(422, {"detail": [1, "x", 2]}, error_type="invalid_input")
    assert err.message == "HTTP 422"
    assert "[" not in err.message
    assert err.validation_errors == ()


# --- a `detail[]` array identifies the Router surface even under the
#     status-derived guess a stripped header falls to, or a code this version
#     does not know: the typed entries must not be dropped by the non-Router
#     branch.


def test_a_validation_array_under_a_shared_bucket_keeps_its_entries() -> None:
    import comfy_sdk.router_exceptions as rx

    low = error_from_envelope(
        401,
        {"detail": [{"loc": ["body", "key"], "msg": "field required"}]},
        error_type="unauthorized",
    )
    err = to_sdk_error(low)
    # `unauthorized` is one of the three buckets both surfaces spell alike. They
    # are a single class now (#157), re-exported from both modules, so there is
    # no longer a collision to resolve -- `except RouterError` and
    # `except comfy_sdk.Unauthorized` both catch this. What this test pins is the
    # part that is still this change's: the array's typed entries survive the
    # mapping instead of being dropped on the non-Router branch.
    assert isinstance(err, rx.Unauthorized)
    assert isinstance(err, rx.RouterError)
    assert rx.Unauthorized is SdkUnauthorized
    assert [e.msg for e in err.errors] == ["field required"]
    assert err.errors[0].location == "body.key"


def test_a_stripped_422_header_validation_array_keeps_its_v2_class() -> None:
    import comfy_sdk.router_exceptions as rx
    from comfy_sdk.exceptions import InvalidWorkflow

    # No `error_type`: the 422 falls to the status-derived `invalid_workflow`
    # guess. The `detail[]` array must NOT retype it into the Router hierarchy
    # -- the array is a body shape any proxy or gateway can send, and the module
    # rule is that only a response carrying a BUCKET gets retyped. Rerouting on
    # the array would silently stop `except InvalidWorkflow` from firing.
    low = error_from_envelope(422, {"detail": [{"loc": ["body", "steps"], "msg": "too large"}]})
    assert low.code == "invalid_workflow"
    err = to_sdk_error(low)
    assert isinstance(err, InvalidWorkflow)
    assert not isinstance(err, rx.RouterError)
    # The per-field reasons still reach the caller: `summarise_detail` made them
    # the message one layer down, which is what this change fixed.
    assert "body.steps: too large" in str(err)


def test_a_v2_envelope_with_an_array_keeps_its_class_details_and_summary() -> None:
    import comfy_sdk.router_exceptions as rx
    from comfy_sdk.exceptions import InvalidWorkflow

    # An `error.message` AND a `detail[]` array, under a v2 envelope code. The
    # class stays the one integrators catch, and `details` -- the per-node
    # diagnostics `InvalidWorkflow` documents -- is still forwarded.
    low = error_from_envelope(
        422,
        {
            "error": {
                "code": "invalid_workflow",
                "message": "the graph is invalid",
                "details": {"node_errors": {"3": "bad"}},
            },
            "detail": [{"loc": ["body", "steps"], "msg": "too large"}],
        },
    )
    err = to_sdk_error(low)
    assert isinstance(err, InvalidWorkflow)
    assert not isinstance(err, rx.RouterError)
    assert err.details == {"node_errors": {"3": "bad"}}
    # The envelope's own message still wins as the human-readable string.
    assert "the graph is invalid" in str(err)


def test_a_loc_member_that_is_not_a_scalar_never_leaks_a_repr() -> None:
    # `str(part)` on a nested member would render `['a', 'b']: bad` into the
    # user-visible message -- the same list-repr leak `_clean`'s isinstance
    # guard exists to stop, and `clean_body_excerpt` does not strip brackets.
    err = error_from_envelope(
        422,
        {"detail": [{"loc": ["body", ["a", "b"], 0], "msg": "bad"}]},
        error_type="invalid_input",
    )
    assert "[" not in err.message
    assert err.message == "body.0: bad"


def test_a_huge_detail_array_stops_accumulating_at_the_excerpt_budget() -> None:
    from comfy_low.errors import _BODY_EXCERPT_LIMIT

    # The cap must not be reached by rendering the whole server-controlled array
    # and slicing the tail off: describing a body costs the same whether it is
    # small or huge.
    entries = [{"msg": "x" * 100} for _ in range(10_000)]
    err = error_from_envelope(422, {"detail": entries}, error_type="invalid_input")
    assert len(err.message) == _BODY_EXCERPT_LIMIT


def test_an_unprintable_entry_does_not_spend_the_budget_it_cannot_fill() -> None:
    from comfy_low.errors import summarise_detail

    # The budget stops accumulation once there is enough to FILL the 256-char
    # cap, so only text that survives the reduction may be charged to it. A
    # control-only `msg` survives `_clean` -- `str.strip()` does not treat NUL
    # as whitespace -- but sanitises away to nothing, so charging its raw length
    # let one such entry exhaust the budget on its own, break the loop, and then
    # contribute no output at all. Every readable entry behind it was lost and
    # the caller got a bare `HTTP 422` -- the exact failure `summarise_detail`
    # was written to end, reachable by anything that can set a response body.
    entries = [{"msg": "\x00" * 2048}, {"loc": ["body", "seed"], "msg": "field required"}]
    assert summarise_detail(entries) == "body.seed: field required"

    err = error_from_envelope(422, {"detail": entries}, error_type="invalid_input")
    assert err.message == "body.seed: field required"
    # The raw entries are unaffected either way -- they are data, not display.
    assert len(err.validation_errors) == 2

    # The same for a run of them, and for the interleaved case: an entry that
    # reduces to nothing is skipped, never merely truncated into the output.
    # Control characters and a bidi override only -- an ANSI sequence would be
    # a poor probe here, since only its ESC is unprintable and the `[31m` that
    # follows is ordinary text that should and does survive.
    noise = {"msg": "\x00\x01\u202e\x7f" * 512}
    assert summarise_detail([noise] * 20 + [{"msg": "real"}]) == "real"
    assert summarise_detail([{"msg": "a"}, noise, {"msg": "b"}]) == "a; b"
    assert summarise_detail([noise] * 50) is None


def test_a_shared_bucket_without_an_array_still_maps_by_code() -> None:
    # The array is not what types a shared bucket: a bare `unauthorized` with no
    # `detail[]` still maps by code to the one class both surfaces export.
    err = to_sdk_error(ApiError("no", code="unauthorized", http_status=401))
    assert isinstance(err, SdkUnauthorized)


# --- the string forms are reduced too, not only the `detail[]` summary ---
#
# `error.message` and Router's request-level string `detail` are as
# server-controlled as any body excerpt, and both become `str(exc)`. They used
# to be merely `.strip()`ed, so an intermediary could put escape sequences and
# ten thousand characters into whatever printed the exception.

#: One string carrying every category the reduction exists for: an ANSI colour
#: sequence (``Cc``), a newline that would break a one-line log record, a
#: right-to-left override (``Cf``) that reverses how the rest reads, a NUL, and
#: enough padding to flood the line it lands on.
HOSTILE = "\x1b[31mBAD\x1b[0m\n\u202ereversed\x00" + "x" * 10_000


def assert_bounded_and_printable(text: str) -> None:
    """``text`` is one printable line no longer than the excerpt limit."""
    import unicodedata

    from comfy_low.errors import _BODY_EXCERPT_LIMIT

    assert "\n" not in text
    assert "\x1b" not in text
    assert "\u202e" not in text
    assert len(text) <= _BODY_EXCERPT_LIMIT
    assert all(unicodedata.category(ch) not in {"Cc", "Cf", "Co", "Cs"} for ch in text)


@pytest.mark.parametrize(
    ("body", "kwargs"),
    [
        pytest.param(
            {"detail": HOSTILE, "error_type": "provider_error"},
            {"error_type": "provider_error"},
            id="router-string-detail",
        ),
        pytest.param(
            {"error": {"code": "boom", "message": HOSTILE}},
            {},
            id="v2-error-message",
        ),
    ],
)
def test_a_hostile_string_message_is_bounded_and_printable(body: dict, kwargs: dict) -> None:
    from comfy_low.errors import _BODY_EXCERPT_LIMIT

    status = 502 if "detail" in body else 500
    err = error_from_envelope(status, body, **kwargs)

    assert_bounded_and_printable(err.message)
    assert_bounded_and_printable(str(err))
    # Exactly the cap, not merely under it: the padding is long enough that a
    # reduction which silently stopped short would still pass the bound above.
    assert len(err.message) == _BODY_EXCERPT_LIMIT
    # The message-found gate still holds -- a response that said something does
    # not also carry a second copy of it as an excerpt.
    assert err.body_excerpt is None


def test_a_sanitised_message_keeps_the_words_the_server_sent() -> None:
    # Reduction, not redaction: the unprintable characters become spaces and the
    # text around them survives, so the reason is still readable.
    err = error_from_envelope(
        502, {"detail": "no healthy\x00upstream\n"}, error_type="provider_error"
    )
    assert err.message == "no healthy upstream"


@pytest.mark.parametrize("raw", ["   ", "\n\t", "\u200b"])
def test_a_blank_string_detail_still_falls_back_to_the_status(raw: str) -> None:
    # Whitespace-only, and the zero-width space that reduces to whitespace: each
    # reads as absent, exactly as it did when `_clean` did the reading, so the
    # status-derived default fires rather than a blank description.
    err = error_from_envelope(502, {"detail": raw}, error_type="provider_error")
    assert err.message == "HTTP 502"
    assert err.error_type == "provider_error"


def test_a_hostile_message_does_not_disturb_the_code_fields() -> None:
    # `clean_body_excerpt` is for the display text only. The code fields stay on
    # `_clean`, so a wire token is never whitespace-collapsed or capped.
    err = error_from_envelope(500, {"error": {"code": "  boom  ", "message": HOSTILE}})
    assert err.code == "boom"
