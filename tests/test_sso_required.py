"""``sso_required``: a valid key whose account must sign in through its org's SSO.

The gateway answers ``403`` with ``{"error": {"code": "sso_required", "message":
..., "organization_id": "org_..."}}`` on every v2 operation when a personal key
belongs to an account an SSO organization holds. ``organization_id`` is a sibling
of ``code`` inside ``error`` and is absent when the gateway does not know the org.

It raises ``Forbidden`` — so an auth ``except`` written before the code existed
still catches it — with ``code`` kept as ``sso_required`` and the organization on
``organization_id``. There is deliberately no ``SsoRequired`` class: the SDK's
``Forbidden`` is a Router bucket class and that surface is one class per bucket.
"""

from __future__ import annotations

import pytest

from comfy_low.errors import ApiError, error_from_envelope
from comfy_low.errors import Forbidden as LowForbidden
from comfy_sdk import AsyncComfy, Comfy
from comfy_sdk.exceptions import ComfyError, Forbidden, QueueFull, to_sdk_error, translating
from comfy_sdk.router_exceptions import ROUTER_ERROR_TYPES, RouterError, exception_for

ORG = "org_01HXYZEXAMPLE"


def _sso_body(**extra: object) -> dict:
    return {"error": {"code": "sso_required", "message": "sign in with SSO", **extra}}


# --- comfy_low: the envelope is read ---


def test_sso_required_is_a_low_forbidden_carrying_the_org() -> None:
    err = error_from_envelope(403, _sso_body(organization_id=ORG))
    assert isinstance(err, LowForbidden)
    assert err.code == "sso_required"
    assert err.organization_id == ORG
    assert err.message == "sign in with SSO"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"organization_id": ""},
        {"organization_id": "   "},
        {"organization_id": 7},
        {"organization_id": "org_1&next=https://evil.example"},
        {"organization_id": "org_1#frag"},
        {"organization_id": "org\x1b[2Jx"},
        {"organization_id": "org\u202e1"},
        {"organization_id": "o" * 201},
    ],
    ids=[
        "absent",
        "empty",
        "blank",
        "non-string",
        "url-delimiter",
        "fragment",
        "control-char",
        "bidi-override",
        "overlong",
    ],
)
def test_an_unknown_org_reads_as_none(extra: dict) -> None:
    err = error_from_envelope(403, _sso_body(**extra))
    assert isinstance(err, LowForbidden)
    assert err.code == "sso_required"
    assert err.organization_id is None


def test_a_surrounding_space_is_stripped_from_the_org() -> None:
    assert error_from_envelope(403, _sso_body(organization_id=f"  {ORG} ")).organization_id == ORG


@pytest.mark.parametrize(
    ("status", "code"), [(404, "not_found"), (429, "queue_full"), (403, "forbidden")]
)
def test_the_org_is_read_only_off_an_sso_required_envelope(status: int, code: str) -> None:
    # The documented contract is "None on every other code": a caller that
    # starts SSO whenever the field is set must not be steered by an
    # unrelated error body that happens to carry it.
    body = {"error": {"code": code, "message": "no", "organization_id": ORG}}
    assert error_from_envelope(status, body).organization_id is None


def test_a_plain_forbidden_carries_no_org() -> None:
    err = error_from_envelope(403, {"error": {"code": "forbidden", "message": "no"}})
    assert isinstance(err, LowForbidden)
    assert err.organization_id is None


def test_a_hand_built_api_error_defaults_the_org_to_none() -> None:
    assert ApiError("boom", http_status=500).organization_id is None


# --- comfy_sdk: the translation boundary forwards it ---


def test_to_sdk_error_raises_forbidden_and_forwards_the_org() -> None:
    err = to_sdk_error(error_from_envelope(403, _sso_body(organization_id=ORG)))
    assert isinstance(err, Forbidden)
    assert isinstance(err, RouterError)
    assert err.code == "sso_required"
    assert err.http_status == 403
    assert err.organization_id == ORG


def test_the_bucket_stays_forbidden_while_the_code_says_sso_required() -> None:
    # `error_type` is the Router bucket and `sso_required` is not one: the
    # instance keeps its class's bucket so it still compares equal to
    # `Forbidden.error_type` and round-trips through `exception_for`.
    err = to_sdk_error(error_from_envelope(403, _sso_body(organization_id=ORG)))
    assert err.error_type == Forbidden.error_type == "forbidden"
    assert err.error_type in ROUTER_ERROR_TYPES
    assert exception_for(err.error_type) is Forbidden
    assert err.code == "sso_required"


def test_a_plain_forbidden_keeps_code_and_bucket_equal() -> None:
    err = to_sdk_error(error_from_envelope(403, {"error": {"code": "forbidden", "message": "no"}}))
    assert isinstance(err, Forbidden)
    assert err.code == err.error_type == "forbidden"


def test_an_unknown_bucket_still_carries_what_it_was_sent() -> None:
    err = to_sdk_error(error_from_envelope(418, {"detail": "teapot", "error_type": "brand_new"}))
    assert type(err) is RouterError
    assert err.code == err.error_type == "brand_new"


def test_to_sdk_error_forwards_the_org_on_the_generic_branch() -> None:
    low = ApiError("gone", code="not_found", http_status=404, organization_id=ORG)
    err = to_sdk_error(low)
    assert type(err).__name__ == "NotFound"
    assert err.organization_id == ORG


def test_to_sdk_error_forwards_the_org_on_the_queue_full_branch() -> None:
    low = ApiError("busy", code="queue_full", http_status=429, retry_after=3, organization_id=ORG)
    err = to_sdk_error(low)
    assert isinstance(err, QueueFull)
    assert err.organization_id == ORG


def test_the_base_error_defaults_the_org_to_none() -> None:
    assert ComfyError("boom").organization_id is None
    assert RouterError("boom", http_status=500).organization_id is None


def test_translating_keeps_the_org_when_it_stamps_a_key() -> None:
    with pytest.raises(Forbidden) as excinfo:
        with translating(idempotency_key="k-sso"):
            raise error_from_envelope(403, _sso_body(organization_id=ORG))
    assert excinfo.value.organization_id == ORG
    assert excinfo.value.idempotency_key == "k-sso"


# --- end to end, against the stub ---


def test_a_sync_client_raises_forbidden_with_the_org(server) -> None:
    server.state.sso_required = {"organization_id": ORG}
    server.state.require_auth = True
    with Comfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            client.jobs.get("any")
    assert e.value.code == "sso_required"
    assert e.value.http_status == 403
    assert e.value.organization_id == ORG
    assert server.state.last_auth_header == "Bearer ck_personal"


def test_a_sync_client_reads_an_unknown_org_as_none(server) -> None:
    server.state.sso_required = {"organization_id": None}
    server.state.require_auth = True
    with Comfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            client.jobs.get("any")
    assert e.value.code == "sso_required"
    assert e.value.organization_id is None
    assert server.state.last_auth_header == "Bearer ck_personal"


def test_a_submit_is_refused_the_same_way(server) -> None:
    server.state.sso_required = {"organization_id": ORG}
    server.state.require_auth = True
    with Comfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            client.submit(client.workflows.from_json({"3": {"class_type": "K", "inputs": {}}}))
    assert e.value.code == "sso_required"
    assert e.value.organization_id == ORG
    assert server.state.last_auth_header == "Bearer ck_personal"


async def test_an_async_client_raises_forbidden_with_the_org(server) -> None:
    server.state.sso_required = {"organization_id": ORG}
    server.state.require_auth = True
    async with AsyncComfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            await client.jobs.get("any")
    assert e.value.code == "sso_required"
    assert e.value.organization_id == ORG
    assert server.state.last_auth_header == "Bearer ck_personal"
