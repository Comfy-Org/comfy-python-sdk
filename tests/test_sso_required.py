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
from comfy_sdk.router_exceptions import RouterError

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
    [{}, {"organization_id": ""}, {"organization_id": "   "}, {"organization_id": 7}],
    ids=["absent", "empty", "blank", "non-string"],
)
def test_an_unknown_org_reads_as_none(extra: dict) -> None:
    err = error_from_envelope(403, _sso_body(**extra))
    assert isinstance(err, LowForbidden)
    assert err.code == "sso_required"
    assert err.organization_id is None


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
    with Comfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            client.jobs.get("any")
    assert e.value.code == "sso_required"
    assert e.value.http_status == 403
    assert e.value.organization_id == ORG


def test_a_sync_client_reads_an_unknown_org_as_none(server) -> None:
    server.state.sso_required = {"organization_id": None}
    with Comfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            client.jobs.get("any")
    assert e.value.code == "sso_required"
    assert e.value.organization_id is None


def test_a_submit_is_refused_the_same_way(server) -> None:
    server.state.sso_required = {"organization_id": ORG}
    with Comfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            client.submit(client.workflows.from_json({"3": {"class_type": "K", "inputs": {}}}))
    assert e.value.code == "sso_required"
    assert e.value.organization_id == ORG


async def test_an_async_client_raises_forbidden_with_the_org(server) -> None:
    server.state.sso_required = {"organization_id": ORG}
    async with AsyncComfy(api_key="ck_personal") as client:
        with pytest.raises(Forbidden) as e:
            await client.jobs.get("any")
    assert e.value.code == "sso_required"
    assert e.value.organization_id == ORG
