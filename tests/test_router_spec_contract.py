"""The router binding is the vendored contract's, not this repo's.

Two things are read straight out of ``spec/router-openapi.yaml`` rather than
restated here:

* the closed error set it declares as ``x-comfy-error-types`` -- one entry per
  wire value, each with the ``meaning`` prose the exception docstrings
  reproduce -- compared against :mod:`comfy_sdk.router_exceptions`, both for
  the values and their order and, per bucket, for whether that prose has moved
  since its docstring was written;
* the **route** ``post_model_run`` is bound to -- the path whose
  ``post.operationId`` is ``runRouterModel``, and the ``servers[0].url`` it is
  addressed against -- compared against
  :data:`comfy_low.transport._MODEL_RUN_PATH_TEMPLATE` and
  :data:`comfy_sdk.COMFY_ROUTER_BASE_URL`.

Neither is generated, so a Router spec sync is the moment they can drift. The
failures guarded against are a sync landing a new bucket that then reaches
callers as an untyped ``RouterError``, and a sync **moving the path** (the
``/v1`` -> ``/v2`` move already on the roadmap) while the constant the SDK
posts to stays where it was -- with nothing going red either time.

That is also why the assertions are written against the file rather than
against a list copied out of it. A test that restated the set would pass a sync
it should have failed.

``scripts/check_drift.py`` runs the same comparison in CI's codegen-drift job,
which is the gate that catches it even for someone who only ran the linters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from comfy_low.transport import _MODEL_RUN_PATH_TEMPLATE
from comfy_sdk import COMFY_ROUTER_BASE_URL
from comfy_sdk.models import _run_result
from comfy_sdk.router_exceptions import (
    ROUTER_ERROR_TYPES,
    ROUTER_EXCEPTIONS,
    RouterError,
    _meaning_digest,
    exception_for,
)

ROUTER_SPEC = Path(__file__).resolve().parent.parent / "spec" / "router-openapi.yaml"


def _declared_error_types() -> list[dict[str, Any]]:
    """The ``x-comfy-error-types`` entries, in the order the spec declares them.

    ``encoding="utf-8"`` is explicit: the ``meaning`` prose is not ASCII, and
    the locale default would raise ``UnicodeDecodeError`` on Windows from a file
    that is perfectly fine.
    """
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    entries = doc["components"]["schemas"]["RouterErrorType"]["x-comfy-error-types"]
    assert isinstance(entries, list) and entries, "the vendored spec declares no error types"
    return entries


def _declared_or_empty() -> list[dict[str, Any]]:
    """The entries, or ``[]`` if the spec is missing or reshaped.

    This runs at *import* time, to parametrize the per-bucket tests below, and
    collection-time exceptions are the one failure mode a test file cannot
    report: a missing ``spec/router-openapi.yaml`` would raise
    ``FileNotFoundError`` and a reshaped entry a ``KeyError``, aborting the
    module before ``test_the_vendored_router_spec_is_present`` -- the test
    written to say exactly that -- ever runs. Degrading to an empty list keeps
    collection alive so the precise assertion fires;
    ``test_the_spec_declares_at_least_one_bucket`` is what stops an empty list
    from reading as a clean pass, since a parametrized test with no cases is
    silently green.
    """
    try:
        return _declared_error_types()
    except Exception:
        return []


DECLARED = _declared_or_empty()
# `isinstance(entry, dict)` before `.get`, for the same reason the load above is
# guarded: an entry that is a bare string rather than a mapping would raise
# `AttributeError` *here*, at import, and abort collection all over again.
# Filtering silently is safe because it can only make this list shorter, and
# `test_the_class_count_equals_the_spec_s_list_length` fails when it is.
DECLARED_VALUES = [
    entry["value"]
    for entry in DECLARED
    if isinstance(entry, dict) and isinstance(entry.get("value"), str)
]
# The same filter, one field wider, for the tests that read `tier` and
# `meaning`. Built at import for the same reason and with the same guard: an
# entry missing either field would raise here rather than fail a test, and
# `test_the_spec_states_a_meaning_and_a_tier_for_every_bucket` is what refuses
# to let a filtered-out entry read as a pass.
DECLARED_ENTRIES = [
    entry
    for entry in DECLARED
    if isinstance(entry, dict)
    and isinstance(entry.get("value"), str)
    and isinstance(entry.get("tier"), str)
    and isinstance(entry.get("meaning"), str)
]


def test_the_vendored_router_spec_is_present() -> None:
    # The whole point of vendoring it: the next sync is a diff against this
    # file rather than a first import nobody reviewed.
    assert ROUTER_SPEC.is_file()
    # Re-read rather than trusting `DECLARED`: this is the test that reports
    # *why* the file is unusable, so it has to see the real exception.
    assert _declared_error_types()


def test_the_spec_declares_at_least_one_bucket() -> None:
    # The backstop for the degradation above. Every per-value test below is
    # parametrized over `DECLARED_VALUES`, and pytest reports a parametrized
    # test with zero cases as passing -- so without this, a spec that failed to
    # load would take the whole contract check green.
    assert DECLARED_VALUES, (
        "no x-comfy-error-types entries could be read from the vendored spec, "
        "so every per-bucket test below was parametrized empty and passed vacuously"
    )


@pytest.mark.parametrize("value", DECLARED_VALUES)
def test_every_declared_bucket_has_a_class(value: str) -> None:
    cls = exception_for(value)
    assert cls is not RouterError, (
        f"the vendored spec declares {value!r} and the SDK has no class for it -- "
        "a caller can only reach it as the base class"
    )
    assert cls.error_type == value


def test_the_closed_set_is_the_spec_s_list_in_the_spec_s_order() -> None:
    # Order too, not just membership: `ROUTER_EXCEPTIONS` is documented as the
    # declaration order, and both SDKs present the set in it.
    assert list(ROUTER_ERROR_TYPES) == DECLARED_VALUES


def test_the_class_count_equals_the_spec_s_list_length() -> None:
    assert len(ROUTER_EXCEPTIONS) == len(DECLARED_VALUES)
    assert len(ROUTER_ERROR_TYPES) == len(DECLARED_VALUES)


def test_no_class_claims_a_bucket_the_spec_does_not_declare() -> None:
    # The other direction: a hand-added class for a bucket that never made the
    # contract is a name the TypeScript twin will not have.
    assert set(ROUTER_ERROR_TYPES) <= set(DECLARED_VALUES)


@pytest.mark.parametrize("value", DECLARED_VALUES)
def test_every_class_documents_its_bucket(value: str) -> None:
    # The `meaning` prose is the only place the difference between two buckets
    # that share a status is written down, so a class without a docstring is a
    # class whose whole reason for existing is missing.
    doc = exception_for(value).__doc__
    assert doc and doc.strip(), f"{value!r} has a class with no docstring"


def test_the_spec_states_a_meaning_and_a_tier_for_every_bucket() -> None:
    # Guards the reader of this file as much as the SDK: the assertions above
    # are only as good as the entries they read.
    for entry in DECLARED:
        assert isinstance(entry, dict), f"x-comfy-error-types entry is not a mapping: {entry!r}"
        assert entry.get("tier") in {"request", "transport"}, entry
        assert isinstance(entry.get("meaning"), str) and entry["meaning"].strip(), entry


def test_every_request_tier_bucket_precedes_every_transport_tier_one() -> None:
    """The assumption that lets the order check above stand in for a tier check.

    Nothing else in this file reads ``tier``: the class table is compared as a
    flat ordered list, which only carries the request/transport split as long
    as the spec keeps the two runs contiguous and request-first. A sync that
    interleaved them would leave the section comments in
    ``router_exceptions.py`` describing an order the spec no longer declares,
    with every other assertion here green.
    """
    tiers = [entry["tier"] for entry in DECLARED_ENTRIES]
    first_transport = tiers.index("transport") if "transport" in tiers else len(tiers)
    assert "request" not in tiers[first_transport:], (
        "the spec's x-comfy-error-types no longer declares every `request`-tier bucket "
        f"before every `transport`-tier one: {tiers} -- the flat order comparison in this "
        "file no longer implies the tier split the section comments in "
        "src/comfy_sdk/router_exceptions.py describe"
    )


@pytest.mark.parametrize(
    "entry", DECLARED_ENTRIES, ids=[entry["value"] for entry in DECLARED_ENTRIES]
)
def test_every_class_is_blessed_against_the_spec_s_current_meaning(
    entry: dict[str, Any],
) -> None:
    """The read marker: this docstring was written against this ``meaning``.

    Deliberately not a comparison against the docstring -- the docstrings
    reword the spec's prose into reST, so equality is impossible by design.
    The digest only answers whether the prose moved since someone last read it.
    """
    cls = exception_for(entry["value"])
    # A bucket the SDK has no class for resolves to the `RouterError` BASE, and
    # the failure below would then tell the developer to set
    # `_spec_meaning_digest` on it -- blessing the base, which every subclass
    # would inherit, and which this module exists to prevent. `check_drift.py`
    # cannot reach that state because its digest pass runs only once the value
    # lists match; these parametrized cases have no such ordering, so the guard
    # is explicit here. Reporting the missing class is
    # `test_every_declared_bucket_has_a_class`'s job, not this one's.
    assert cls is not RouterError, (
        f"{entry['value']!r} is declared in spec/router-openapi.yaml but has no RouterError "
        "subclass in src/comfy_sdk/router_exceptions.py -- add it (see "
        "test_every_declared_bucket_has_a_class). Do NOT set _spec_meaning_digest on "
        "RouterError itself: every bucket would inherit the blessing."
    )
    expected = _meaning_digest(entry["meaning"])
    # `cls.__dict__.get(...)` rather than `getattr`: the invariant is that a
    # class carries its OWN marker, and `getattr` walks the MRO, so a future
    # bucket derived from another bucket would inherit that class's blessing
    # for prose nobody read.
    assert cls.__dict__.get("_spec_meaning_digest") == expected, (
        f"spec/router-openapi.yaml's `meaning` for {entry['value']!r} is not the prose "
        f"{cls.__name__}'s docstring in src/comfy_sdk/router_exceptions.py was written "
        "against -- it changed, or this class was never blessed. Re-read that docstring "
        "against the entry's `meaning` and update it if the semantics moved, then set "
        f'_spec_meaning_digest: str = "{expected}" on the class to record that.'
    )


# --- the route the SDK posts a model run to -----------------------------


def test_run_path_matches_vendored_spec() -> None:
    """The bound path and host are the spec's, read out of it rather than restated.

    Written as a search for the ``operationId`` rather than a lookup of the
    path we expect: looking the path up by name would pass vacuously the day a
    sync moves it, which is the one day this test exists for.
    """
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    declared = [
        path
        for path, item in (doc.get("paths") or {}).items()
        if isinstance(item, dict)
        and isinstance(item.get("post"), dict)
        and item["post"].get("operationId") == "runRouterModel"
    ]
    assert declared == [_MODEL_RUN_PATH_TEMPLATE], (
        f"the vendored spec declares runRouterModel at {declared} and the SDK posts to "
        f"{_MODEL_RUN_PATH_TEMPLATE!r} -- update comfy_low.transport._MODEL_RUN_PATH_TEMPLATE"
    )
    servers = doc.get("servers") or []
    declared_host = servers[0].get("url") if servers else None
    assert declared_host == COMFY_ROUTER_BASE_URL, (
        f"the vendored spec's servers[0].url is {declared_host!r} and the SDK defaults to "
        f"{COMFY_ROUTER_BASE_URL!r} -- update comfy_low.transport.ROUTER_BASE_URL"
    )


def test_the_bound_path_has_exactly_the_two_segments_the_binding_fills() -> None:
    # `model_run_request` fills `{provider}` and `{model}` by name; a sync that
    # renamed or added a template variable would silently KeyError at call time
    # rather than here.
    assert _MODEL_RUN_PATH_TEMPLATE.count("{") == 2
    assert "{provider}" in _MODEL_RUN_PATH_TEMPLATE
    assert "{model}" in _MODEL_RUN_PATH_TEMPLATE


# --- run_detailed's header lifts, pinned against the contract -----------------
#
# `RouterRunResult` is built entirely out of response header names. A name is
# not type-checked, not exercised by a stub that was handed the SDK's own
# spelling, and wrong in a way that looks exactly like the header being absent
# -- which the field documents as a legitimate, common case. So a misspelling
# is silent in every other test in the suite, and it has already happened once:
# the lift read `X-Comfy-Idempotent-Replayed`, a name the contract does not
# use, leaving `replayed` permanently `False` against a real deployment.
#
# These tests close that gap from both ends: the name must be declared by the
# spec, AND the lift must actually be reading that declared name.

#: field on :class:`RouterRunResult` -> the 200 response header it is lifted
#: from, for the lifts whose names the vendored contract declares.
_CONTRACT_HEADER_LIFTS = {
    "serving_provider": "X-Comfy-Router-Fallback-Provider",
    "dropped_params": "X-Comfy-Router-Dropped-Params",
    "replayed": "Idempotent-Replayed",
    "request_id": "X-Comfy-Request-Id",
}

#: Lifted by the SDK but NOT declared on the contract's 200 -- see the tripwire
#: test at the bottom of this file.
_UNDECLARED_HEADER_LIFTS = {"credits_used": "X-Comfy-Credits-Used"}


def _declared_run_response_headers() -> set[str]:
    """The header names the spec declares on ``runRouterModel``'s ``200``."""
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    for _path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        post = item.get("post")
        if isinstance(post, dict) and post.get("operationId") == "runRouterModel":
            return set((post["responses"]["200"].get("headers") or {}).keys())
    raise AssertionError("the vendored spec declares no runRouterModel operation")


@pytest.mark.parametrize(("field", "header"), sorted(_CONTRACT_HEADER_LIFTS.items()))
def test_every_lifted_header_is_declared_by_the_contract(field: str, header: str) -> None:
    declared = _declared_run_response_headers()
    assert header in declared, (
        f"RouterRunResult.{field} is lifted from {header!r}, which the vendored spec does "
        f"not declare on runRouterModel's 200. Declared: {sorted(declared)}. Either a sync "
        f"renamed the header or the SDK is reading a name Router never sends."
    )


@pytest.mark.parametrize(("field", "header"), sorted(_CONTRACT_HEADER_LIFTS.items()))
def test_the_lift_actually_reads_the_declared_name(field: str, header: str) -> None:
    """Declaring the right name is half of it; the lift must also read it.

    Asserted through ``_run_result`` rather than by re-reading the source, so
    this fails if the constant above and the code drift apart -- the constant
    is a restatement otherwise, and a restatement would pass the sync it exists
    to fail.
    """
    absent = getattr(_run_result({}, {}), field)
    present = getattr(_run_result({}, {header: "x"}), field)
    assert present != absent, (
        f"_run_result ignored {header!r}: RouterRunResult.{field} read {absent!r} both with "
        f"the header and without it, so the lift is reading some other name."
    )


@pytest.mark.parametrize(("field", "header"), sorted(_UNDECLARED_HEADER_LIFTS.items()))
def test_an_undeclared_lift_stays_undeclared_until_someone_reconciles_it(
    field: str, header: str
) -> None:
    """Tripwire, and deliberately asserting the *absence*.

    ``credits_used`` is lifted from a header the vendored contract does not
    declare anywhere -- the 200's only cost headers are the
    ``X-Committed-Spend-*`` trio, which is a different quantity (USD cents of
    in-flight commitment, not the price of this run). Nothing in the suite can
    catch a wrong name here, because every test configures its stub to emit the
    exact literal the lift reads.

    That gap is tracked, not accepted. This test fails the moment a spec sync
    declares the header, which is the signal to move the entry up into
    ``_CONTRACT_HEADER_LIFTS`` and get it pinned like the rest. It also fails
    if the header is declared under a *different* name for the same quantity,
    because the reconciliation is the same either way.
    """
    declared = _declared_run_response_headers()
    assert header not in declared, (
        f"the vendored spec now declares {header!r}: move {field!r} from "
        f"_UNDECLARED_HEADER_LIFTS into _CONTRACT_HEADER_LIFTS so it is pinned."
    )


def test_the_refusal_subject_stays_undeclared_until_someone_reconciles_it() -> None:
    """Tripwire for ``RouterError.refusal_subject`` / ``REFUSAL_SUBJECTS``.

    The SDK reads ``X-Comfy-Refusal-Subject`` and the body's
    ``refusal_subject``, and lists the documented values in
    ``REFUSAL_SUBJECTS``, but the vendored contract names none of them -- so
    nothing pins that list the way ``ROUTER_ERROR_TYPES`` is pinned. This fails
    the moment a spec sync mentions either name, which is the signal to
    reconcile the header name, the body field and the value list against what
    the contract then says, and replace this with a real pin.
    """
    text = ROUTER_SPEC.read_text(encoding="utf-8")
    for name in ("X-Comfy-Refusal-Subject", "refusal_subject"):
        assert name not in text, (
            f"the vendored Router spec now mentions {name!r}: reconcile "
            f"comfy_sdk.router_exceptions.REFUSAL_SUBJECT_HEADER / REFUSAL_SUBJECTS against "
            f"it and pin them here."
        )
