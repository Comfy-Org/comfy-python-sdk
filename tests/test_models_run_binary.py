"""``client.models.run`` when the model's native output is bytes, not JSON.

Comfy Router forwards a partner model's output *unchanged*, under the partner's
own media type. For most of the catalog that is a JSON document; for a model
whose partner answers a generation directly as bytes — the ElevenLabs audio
models are the first of these — it is raw audio under `audio/mpeg` (or whatever
the requested output format is). The run route's published ``200`` declares both
branches, ``application/json`` and a ``*/*`` ``format: binary`` one, and says in
as many words that a client MUST branch on the response ``Content-Type``.

Before this, the SDK called ``.json()`` on every 2xx and raised
``invalid_response`` — so a generation that had already run, and already been
billed, was thrown away by the client that asked for it. That is the failure
these tests pin closed, from both clients.

Two boundaries are as important as the happy path and are asserted here:

* the JSON branch is untouched — a 200 whose ``Content-Type`` says JSON and
  whose body will not parse is still the interstitial/truncation error it was,
  because on *that* branch the response promised a document and did not deliver
  one; and
* the binary branch still runs inside ``translating(...)``, so the
  ``Idempotency-Key`` that is a caller's only handle on an already-billed
  generation rides out on a failure and a replayed binary 200 comes back the
  same way a first run does.

Everything here runs against the stubbed server in ``conftest.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from comfy_low.transport import is_json_media_type, media_type
from comfy_sdk import NO_RETRY, AsyncComfy, BinaryResult, Comfy
from comfy_sdk.exceptions import ComfyError

MODEL = "acme/flux-dev"
ARGS = {"prompt": "a cat", "steps": 4}

#: A body shaped like the one the failure was reported on: an ID3v2 header
#: followed by an MP3 frame sync (`0xff 0xfb`). The `0xff` is what
#: `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff` was raised on, so
#: a regression here fails exactly the way the original report did.
AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 35 + b"\xff\xfb\x90\x64" + b"\xde\xad" * 64


def _serve_audio(server, content_type: str | None = "audio/mpeg") -> None:
    server.state.model_run_binary_body = AUDIO
    server.state.model_run_binary_content_type = content_type


# --- the reported failure: an audio/mpeg 200 is a result, not an error ----


def test_an_audio_200_returns_the_bytes_verbatim(server) -> None:
    _serve_audio(server)
    server.state.model_run_request_id = "req_audio_01"
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.content == AUDIO
    assert result.content_type == "audio/mpeg"
    assert result.request_id == "req_audio_01"
    # The generation was billed once and handed back once — the whole point.
    assert server.state.model_run_generations == 1


async def test_the_async_client_returns_the_same_binary_result(server) -> None:
    _serve_audio(server)
    server.state.model_run_request_id = "req_audio_01"
    async with AsyncComfy(retry=NO_RETRY) as client:
        result = await client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.content == AUDIO
    assert result.content_type == "audio/mpeg"
    assert result.request_id == "req_audio_01"


def test_a_binary_200_raises_nothing(server) -> None:
    # Stated on its own because the defect was an *exception*, not a wrong
    # value: `pytest.raises` passing elsewhere is not the same assertion.
    _serve_audio(server)
    with Comfy(retry=NO_RETRY) as client:
        client.models.run(MODEL, ARGS)  # no exception


def test_a_response_with_no_request_id_header_gives_none(server) -> None:
    _serve_audio(server)
    server.state.model_run_request_id = None
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.request_id is None


def test_a_created_shaped_binary_success_is_also_a_result(server) -> None:
    # 201 is in the route's ok set alongside 200; the branch is on the media
    # type, not on which success status carried it.
    _serve_audio(server)
    server.state.model_run_status = 201
    with Comfy(retry=NO_RETRY) as client:
        assert isinstance(client.models.run(MODEL, ARGS), BinaryResult)


# --- the media type is carried verbatim, parameters included --------------


@pytest.mark.parametrize(
    "content_type",
    [
        "audio/mpeg",
        # The format ElevenLabs returns for a `pcm_*` output_format: the
        # parameter is not decoration, it is how many samples a second the
        # bytes are. Dropping it would leave the caller holding unplayable PCM.
        "audio/L16; rate=16000",
        "audio/wav",
        "application/octet-stream",
        "image/png",
        "video/mp4",
    ],
)
def test_the_content_type_reaches_the_caller_unchanged(server, content_type: str) -> None:
    _serve_audio(server, content_type)
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.content_type == content_type
    assert result.content == AUDIO


def test_a_json_content_type_with_a_charset_is_still_the_dict_branch(server) -> None:
    # `application/json; charset=utf-8` is JSON. Branching on the raw header
    # rather than its media type would have sent it down the binary path.
    server.state.model_run_binary_body = None
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert result == server.state.model_run_result


def test_a_json_suffix_media_type_is_the_dict_branch(server) -> None:
    # RFC 6839's `+json` structured suffix — a partner answering under its own
    # vendor media type is still handing back a JSON document.
    server.state.model_run_binary_body = b'{"ok": true}'
    server.state.model_run_binary_content_type = "application/vnd.acme.result+json"
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert result == {"ok": True}


# --- the JSON branch keeps the reading the binary branch takes away -------


def test_a_json_content_type_whose_body_will_not_parse_is_still_an_error(server) -> None:
    # The "interstitial served as 200" reading belongs *here* and only here:
    # the response promised a JSON document and did not deliver one, so there
    # is nothing to hand back. The key still rides out on it.
    server.state.model_run_undecodable_body = True
    server.state.model_run_undecodable_content_type = "application/json"
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.code == "invalid_response"
    assert excinfo.value.http_status == 200
    assert excinfo.value.idempotency_key is not None


async def test_the_async_json_branch_raises_the_same_way(server) -> None:
    server.state.model_run_undecodable_body = True
    server.state.model_run_undecodable_content_type = "application/json"
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.models.run(MODEL, ARGS)
    assert excinfo.value.code == "invalid_response"
    assert excinfo.value.idempotency_key is not None


def test_a_text_html_200_now_comes_back_as_bytes(server) -> None:
    # The deliberate consequence of branching on the declared type: an HTML
    # body under a 200 is no longer read as a proxy interstitial. The SDK
    # cannot tell one from a partner's own text output, and on this route the
    # contract says the body is the partner's — so throwing away a generation
    # the caller was billed for is the worse of the two mistakes. The bytes,
    # interstitial or not, reach the caller, who can see what answered.
    server.state.model_run_undecodable_body = True
    server.state.model_run_undecodable_content_type = "text/html"
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.content_type == "text/html"
    assert b"502 from an intermediary" in result.content


# --- a success that names no media type at all ---------------------------


def test_no_content_type_and_an_unparseable_body_is_a_binary_result(server) -> None:
    # Nothing to branch on, so the body decides — and an empty `content_type`
    # says the response named none rather than naming the wrong one.
    _serve_audio(server, content_type=None)
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.content == AUDIO
    assert result.content_type == ""


def test_no_content_type_but_a_json_body_is_still_a_dict(server) -> None:
    # The other half of that: a header-stripping intermediary in front of a
    # JSON model must not turn every result into opaque bytes.
    server.state.model_run_binary_body = b'{"images": [], "seed": 7}'
    server.state.model_run_binary_content_type = None
    with Comfy(retry=NO_RETRY) as client:
        result = client.models.run(MODEL, ARGS)
    assert result == {"images": [], "seed": 7}


def test_no_content_type_and_an_empty_body_is_still_an_empty_dict(server) -> None:
    # Unchanged from every other operation: an empty success body is `{}`, not
    # a `BinaryResult` holding zero bytes.
    server.state.model_run_binary_body = b""
    server.state.model_run_binary_content_type = None
    with Comfy(retry=NO_RETRY) as client:
        assert client.models.run(MODEL, ARGS) == {}


# --- the key still rides out, and a replay is a result ---------------------


def test_a_binary_run_still_sends_and_records_an_idempotency_key(server) -> None:
    _serve_audio(server)
    with Comfy(retry=NO_RETRY) as client:
        client.models.run(MODEL, ARGS)
    assert server.state.model_run_idempotency_keys[-1]


def test_a_failure_on_the_binary_path_still_carries_the_key(server) -> None:
    # The binary branch does not move the call out of `translating(...)`: a
    # model that answers in bytes still fails like any other, and the key is
    # still the caller's handle on a generation they may already owe for.
    _serve_audio(server)
    server.state.model_run_error = (503, "internal_error")
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
    assert excinfo.value.idempotency_key is not None
    assert excinfo.value.idempotency_key == server.state.model_run_idempotency_keys[0]


def test_a_replayed_binary_200_is_returned_like_a_first_run(server) -> None:
    # The replay contract end to end: the first attempt's response is lost
    # (a 5xx after the generation completed), the caller resends the key they
    # were handed, and the recorded result comes back under
    # `Idempotent-Replayed` — as the same bytes, not as an exception, and
    # without the model running a second time.
    _serve_audio(server)
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_replays_lost_result = True
    with Comfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            client.models.run(MODEL, ARGS)
        key = excinfo.value.idempotency_key
        assert key is not None

        server.state.model_run_error = None
        replayed = client.models.run(MODEL, ARGS, idempotency_key=key)

    assert isinstance(replayed, BinaryResult)
    assert replayed.content == AUDIO
    assert replayed.content_type == "audio/mpeg"
    # Billed once across both requests: the replay served the record.
    assert server.state.model_run_count == 2
    assert server.state.model_run_generations == 1


async def test_the_async_replay_of_a_binary_200_behaves_the_same(server) -> None:
    _serve_audio(server)
    server.state.model_run_error = (504, "deadline_exceeded")
    server.state.model_run_replays_lost_result = True
    async with AsyncComfy(retry=NO_RETRY) as client:
        with pytest.raises(ComfyError) as excinfo:
            await client.models.run(MODEL, ARGS)
        key = excinfo.value.idempotency_key
        assert key is not None

        server.state.model_run_error = None
        replayed = await client.models.run(MODEL, ARGS, idempotency_key=key)

    assert isinstance(replayed, BinaryResult)
    assert replayed.content == AUDIO
    assert server.state.model_run_generations == 1


def test_a_retry_that_succeeds_binary_returns_the_bytes(server) -> None:
    # The retry loop returns whatever the successful attempt produced; nothing
    # about the binary branch is outside it.
    _serve_audio(server)
    server.state.model_run_fail_times = 1
    server.state.model_run_transient_error = (429, "queue_full")
    server.state.model_run_retry_after = "0"
    with Comfy() as client:
        result = client.models.run(MODEL, ARGS)
    assert isinstance(result, BinaryResult)
    assert result.content == AUDIO
    assert server.state.model_run_count == 2


# --- the result type itself ----------------------------------------------


def test_binary_result_is_frozen_and_exposes_exactly_three_fields() -> None:
    result = BinaryResult(content=b"abc", content_type="audio/mpeg", request_id="req_1")
    assert [f.name for f in dataclasses.fields(result)] == [
        "content",
        "content_type",
        "request_id",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.content = b"xyz"  # type: ignore[misc]


def test_binary_result_repr_does_not_dump_the_body() -> None:
    # This object holds a whole audio file and lands in tracebacks, REPL echoes
    # and CI logs; a dataclass's default repr would print every byte of it.
    result = BinaryResult(content=b"\xff\xfb" * 100_000, content_type="audio/mpeg", request_id=None)
    text = repr(result)
    assert "200000 bytes" in text
    assert "audio/mpeg" in text
    assert len(text) < 200


def test_binary_results_compare_by_value() -> None:
    a = BinaryResult(content=b"abc", content_type="audio/mpeg", request_id="req_1")
    b = BinaryResult(content=b"abc", content_type="audio/mpeg", request_id="req_1")
    assert a == b
    assert a != dataclasses.replace(a, content=b"abd")


def test_binary_result_is_exported_from_both_layers() -> None:
    # A caller needs the name to write `isinstance(result, BinaryResult)`, so
    # it is part of the public surface rather than an implementation detail of
    # the transport.
    import comfy_low
    import comfy_sdk
    import comfy_sdk.models

    assert comfy_sdk.BinaryResult is BinaryResult
    assert comfy_sdk.models.BinaryResult is BinaryResult
    assert comfy_low.BinaryResult is BinaryResult
    assert "BinaryResult" in comfy_sdk.__all__
    assert "BinaryResult" in comfy_low.__all__


# --- the media-type predicate the branch is built on ----------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("application/json", "application/json"),
        ("application/json; charset=utf-8", "application/json"),
        ("  Application/JSON ;charset=UTF-8", "application/json"),
        ("audio/mpeg", "audio/mpeg"),
        ("audio/L16; rate=16000", "audio/l16"),
        ("", ""),
        (None, ""),
    ],
)
def test_media_type_strips_parameters_and_case(header: str | None, expected: str) -> None:
    assert media_type(header) == expected


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ("application/json", True),
        ("application/vnd.acme.result+json", True),
        ("application/ld+json", True),
        ("audio/mpeg", False),
        ("text/html", False),
        ("text/plain", False),
        ("application/octet-stream", False),
        # Not JSON: `application/jsonlines` merely starts with the same string,
        # and a `startswith` test would have decoded it as a document.
        ("application/jsonlines", False),
        ("", False),
    ],
)
def test_is_json_media_type_matches_the_declared_branch(media: str, expected: bool) -> None:
    assert is_json_media_type(media) is expected
