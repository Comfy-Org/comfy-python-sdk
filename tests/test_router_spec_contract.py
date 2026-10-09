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
  :data:`comfy_sdk.COMFY_ROUTER_BASE_URL` -- and, the same way, the two
  discovery routes ``models.list()`` / ``models.schema()`` read, by their
  ``get.operationId`` (``listRouterModels``, ``getRouterModelInputSchema``).

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

from dataclasses import fields
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from comfy_low.transport import (
    _MODEL_CATALOG_PATH,
    _MODEL_RUN_PATH_TEMPLATE,
    _MODEL_SCHEMA_PATH_TEMPLATE,
    model_catalog_path,
)
from comfy_sdk import COMFY_ROUTER_BASE_URL
from comfy_sdk.models import RouterRunResult, _run_result
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


def _declared_get_paths(operation_id: str) -> list[str]:
    """Every path whose ``get.operationId`` is ``operation_id``, in spec order."""
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    return [
        path
        for path, item in (doc.get("paths") or {}).items()
        if isinstance(item, dict)
        and isinstance(item.get("get"), dict)
        and item["get"].get("operationId") == operation_id
    ]


@pytest.mark.parametrize(
    ("operation_id", "bound", "constant"),
    [
        ("listRouterModels", _MODEL_CATALOG_PATH, "_MODEL_CATALOG_PATH"),
        ("getRouterModelInputSchema", _MODEL_SCHEMA_PATH_TEMPLATE, "_MODEL_SCHEMA_PATH_TEMPLATE"),
    ],
)
def test_the_discovery_routes_match_the_vendored_spec(
    operation_id: str, bound: str, constant: str
) -> None:
    # `models.list()` / `models.schema()` are hand-bound exactly as the run
    # route is, so a sync that moves either route has to fail here rather than
    # ship an SDK that GETs a path the server no longer serves.
    declared = _declared_get_paths(operation_id)
    assert declared == [bound], (
        f"the vendored spec declares {operation_id} at {declared} and the SDK reads "
        f"{bound!r} -- update comfy_low.transport.{constant}"
    )


def test_the_catalog_query_parameters_are_the_ones_the_spec_declares() -> None:
    # `model_catalog_path` sends `cursor` and `limit` by name; a sync renaming
    # either would otherwise be silently ignored by the server.
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    params = doc["paths"][_MODEL_CATALOG_PATH]["get"]["parameters"]
    shared = doc["components"]["parameters"]
    names = {
        shared[p["$ref"].rsplit("/", 1)[-1]]["name"] if "$ref" in p else p["name"] for p in params
    }
    assert names == {"cursor", "limit"}
    assert model_catalog_path("c", 5) == f"{_MODEL_CATALOG_PATH}?cursor=c&limit=5"


def test_the_bound_path_has_exactly_the_two_segments_the_binding_fills() -> None:
    # `model_run_request` fills `{provider}` and `{model}` by name; a sync that
    # renamed or added a template variable would silently KeyError at call time
    # rather than here.
    assert _MODEL_RUN_PATH_TEMPLATE.count("{") == 2
    assert "{provider}" in _MODEL_RUN_PATH_TEMPLATE
    assert "{model}" in _MODEL_RUN_PATH_TEMPLATE


# --- the two media types the run route's 200 can answer under ------------


def _run_200_content() -> dict[str, Any]:
    """The ``content`` map of ``runRouterModel``'s ``200``, read out of the spec."""
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    item = (doc.get("paths") or {})[_MODEL_RUN_PATH_TEMPLATE]
    content = item["post"]["responses"]["200"]["content"]
    assert isinstance(content, dict) and content, "runRouterModel's 200 declares no content"
    return content


def test_the_run_200_declares_both_a_json_and_a_binary_branch() -> None:
    """The contract behind ``post_model_run`` returning ``dict | BinaryResult``.

    Read out of the spec rather than restated, for the same reason the route is:
    the day a sync drops the ``*/*`` branch (or adds a third one), the SDK's
    two-way branch is either dead code or newly incomplete, and nothing else in
    the suite would notice — the binary tests drive a *stub*, which asserts the
    SDK's behaviour rather than the server's contract.
    """
    content = _run_200_content()
    assert set(content) == {"application/json", "*/*"}, (
        f"the vendored spec's runRouterModel 200 declares {sorted(content)}; "
        "comfy_low.transport._Prepared.parse_run_result branches on exactly two "
        "cases (JSON -> dict, anything else -> BinaryResult)"
    )


def test_the_binary_branch_is_declared_as_raw_bytes() -> None:
    # `format: binary` is what says the body is bytes rather than a base64
    # string or a JSON document — i.e. that `BinaryResult.content` is the
    # partner's file and needs no decoding on the way out.
    schema = _run_200_content()["*/*"].get("schema") or {}
    assert schema.get("type") == "string"
    assert schema.get("format") == "binary"


def test_the_200_promises_the_headers_a_binary_result_is_built_from() -> None:
    # `BinaryResult.request_id` reads `X-Comfy-Request-Id` off a *success*, and
    # the SDK takes the partner's `Content-Type` at its word — which is only
    # safe because the route sends `X-Content-Type-Options: nosniff`.
    doc = yaml.safe_load(ROUTER_SPEC.read_text(encoding="utf-8"))
    headers = doc["paths"][_MODEL_RUN_PATH_TEMPLATE]["post"]["responses"]["200"]["headers"]
    assert "X-Comfy-Request-Id" in headers
    assert "X-Content-Type-Options" in headers
    assert "Idempotent-Replayed" in headers


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
# These tests close that gap from three ends: the name must be declared by the
# spec, the lift must actually be reading that declared name, AND every
# header-derived field must be listed here to be checked at all.

#: 200 response header -> a header value its field's own normaliser accepts.
#:
#: Keyed by header, not by field, and kept apart from the lift classification
#: below on purpose: the exemption test hands ``_run_result`` these values to
#: prove a ``_NON_HEADER_FIELDS`` entry is not secretly a lift. If the probes
#: lived only in ``_CONTRACT_HEADER_LIFTS``, misfiling a lift as an exemption
#: would drop its valid probe along with its lift entry, the header would be
#: sent as the generic fallback its parser rejects, and the field would read
#: the same with and without it -- so the misfiling would pass the very test
#: meant to catch it.
#:
#: The probe value is per-header rather than one shared literal because the
#: normalisers disagree about what is even a value: ``_credits_used`` reports
#: anything that is not a finite decimal as ``None``, so a generic ``"x"``
#: makes a correct lift look like it read some other name entirely. Each entry
#: is the spec's own ``example`` for that header, rendered as the string it
#: arrives as on the wire (``Idempotent-Replayed`` declares the YAML boolean
#: ``true``), so "a value the contract itself would send" is literal rather
#: than aspirational. ``X-Comfy-Router-Fallback-Provider`` is the one header
#: declaring no example, so its probe is just a provider the spec names
#: elsewhere. The examples are copied, not asserted against: pinning a probe to
#: the spec byte-for-byte would churn this table on an example-only sync while
#: catching nothing, since these tests only ever compare present against
#: absent.
#:
#: Copying the example verbatim is why ``dropped_params`` carries a
#: comma-bearing entry rather than a tidied-up one -- that comma is the
#: property its parser exists to preserve (asserted on the parsed value in
#: ``test_models_run.py``, not here).
_HEADER_PROBES = {
    "X-Comfy-Router-Fallback-Provider": "fal",
    "X-Comfy-Router-Dropped-Params": (
        '["moderation (fal applies its own, non-configurable safety filtering)"]'
    ),
    "Idempotent-Replayed": "true",
    "X-Comfy-Request-Id": "6f1a1a6e-6a53-4a5f-9d3a-2b3b0a1f9c21",
    "X-Comfy-Credits-Used": "12.5",
}

#: field on :class:`RouterRunResult` -> (the 200 response header it is lifted
#: from, that header's probe from ``_HEADER_PROBES``). Every header-derived
#: field belongs here; the completeness test at the bottom of this file is what
#: keeps that true as fields are added.
_CONTRACT_HEADER_LIFTS = {
    field: (header, _HEADER_PROBES[header])
    for field, header in {
        "serving_provider": "X-Comfy-Router-Fallback-Provider",
        "dropped_params": "X-Comfy-Router-Dropped-Params",
        "replayed": "Idempotent-Replayed",
        "request_id": "X-Comfy-Request-Id",
        "credits_used": "X-Comfy-Credits-Used",
    }.items()
}

#: :class:`RouterRunResult` fields that are NOT lifted from a response header,
#: and so are exempt from the completeness test at the bottom of this file.
#:
#: Being listed here exempts a field from BOTH pins above, so the exemption is
#: itself checked -- see
#: ``test_an_exempt_field_is_really_unmoved_by_the_headers_it_skips``.
_NON_HEADER_FIELDS = {"output"}


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


@pytest.mark.parametrize(
    ("field", "header"),
    sorted((field, header) for field, (header, _probe) in _CONTRACT_HEADER_LIFTS.items()),
)
def test_every_lifted_header_is_declared_by_the_contract(field: str, header: str) -> None:
    """The other half of the pin below: Router must actually send this name.

    Reading the declared name is worth nothing if the name is not in the
    contract at all, which is the failure ``credits_used`` shipped with -- a
    lift nothing could check, because every other test in the suite configures
    its stub to emit the exact literal the lift reads. Asserted against the
    vendored spec, so a sync that renames or drops a header fails here rather
    than silently turning the field into a permanent default in production.
    """
    declared = _declared_run_response_headers()
    assert header in declared, (
        f"RouterRunResult.{field} is lifted from {header!r}, which the vendored spec does "
        f"not declare on runRouterModel's 200. Declared: {sorted(declared)}. Either a sync "
        f"renamed the header or the SDK is reading a name Router never sends."
    )


@pytest.mark.parametrize(
    ("field", "header", "probe"),
    sorted((field, header, probe) for field, (header, probe) in _CONTRACT_HEADER_LIFTS.items()),
)
def test_the_lift_actually_reads_the_declared_name(field: str, header: str, probe: str) -> None:
    """Declaring the right name is half of it; the lift must also read it.

    Asserted through ``_run_result`` rather than by re-reading the source, so
    this fails if the constant above and the code drift apart -- the constant
    is a restatement otherwise, and a restatement would pass the sync it exists
    to fail.

    The probe is an ``httpx.Headers`` and not a plain dict because that is what
    production hands ``_run_result`` -- ``Models.run_detailed`` passes the
    transport's own response headers straight through. A dict's ``.get`` is
    case-sensitive; ``httpx.Headers`` is not. Probing with a dict would make a
    spec sync that only re-cased a declared name fail here even though the SDK
    still reads it correctly, and the only way to get green again would be a
    no-op edit to the source spelling.
    """
    absent = getattr(_run_result({}, httpx.Headers()), field)
    present = getattr(_run_result({}, httpx.Headers({header: probe})), field)
    assert present != absent, (
        f"_run_result ignored {header!r}: RouterRunResult.{field} read {absent!r} both with "
        f"the header and without it, so the lift is reading some other name."
    )


def test_every_header_derived_field_is_pinned_against_the_contract() -> None:
    """No lift may escape the two tests above by simply not being listed.

    Both tests above are parametrized over ``_CONTRACT_HEADER_LIFTS``, so a
    field added to :class:`RouterRunResult` without an entry there is pinned by
    nothing -- and a misspelled header name is invisible in every other test in
    the suite, because each one configures its stub to emit the exact literal
    the lift reads. That is not hypothetical: ``credits_used`` landed unpinned,
    under a tripwire asserting the spec did *not* declare
    ``X-Comfy-Credits-Used`` -- and the spec sync that declared it merged 35
    seconds before the lift itself did, so the tripwire was already stale when
    it landed and main went red on the next run.

    So the list is closed from the other end: every field on the dataclass is
    either lifted from a header named here, or named in ``_NON_HEADER_FIELDS``
    as deliberately not a lift. Adding a field forces one of those two, which
    is the decision the tripwire used to defer.
    """
    overlap = set(_CONTRACT_HEADER_LIFTS) & _NON_HEADER_FIELDS
    assert not overlap, (
        f"{sorted(overlap)} are classified as BOTH lifted from a header and not a lift. "
        f"The union below would accept that contradiction silently, and the field would be "
        f"skipped by the exemption test while still being pinned as a lift. Pick one."
    )
    declared = {f.name for f in fields(RouterRunResult)}
    accounted = set(_CONTRACT_HEADER_LIFTS) | _NON_HEADER_FIELDS
    assert declared == accounted, (
        f"RouterRunResult fields and the pinned lift list disagree. Unpinned fields: "
        f"{sorted(declared - accounted)}; listed but not fields: {sorted(accounted - declared)}. "
        f"Add each new field to _CONTRACT_HEADER_LIFTS (with the header it is lifted from) "
        f"or to _NON_HEADER_FIELDS."
    )


def test_an_exempt_field_is_really_unmoved_by_the_headers_it_skips() -> None:
    """``_NON_HEADER_FIELDS`` has to earn the exemption, not just assert it.

    Listing a field there exempts it from BOTH pins above, so on its own it is
    an unverified escape hatch that partly reopens the gap this block exists to
    close -- and it is the *convenient* hatch, because a field lifted from a
    header the vendored spec has not declared yet fails
    ``test_every_lifted_header_is_declared_by_the_contract`` if it is filed
    honestly in ``_CONTRACT_HEADER_LIFTS``. That is not a hypothetical shape:
    it is exactly the state ``credits_used`` was in, and the one-way spec sync
    makes it recurring.

    So the claim is tested: hand ``_run_result`` every header that could move a
    field -- the ones this file pins, plus every other name the contract
    declares on the ``200`` -- and an exempt field must read the same as it
    does against no headers at all. A lift misfiled as an exemption moves, and
    fails here instead of passing silently.

    The one shape this still cannot see is a field lifted from a header that is
    neither pinned here nor declared by the spec, since nothing in the repo
    then knows the name to send. Closing that needs the source read, which the
    rest of this block deliberately refuses to do. A narrower one: a field
    lifted from a header the spec declares but ``_HEADER_PROBES`` has no entry
    for is sent the generic ``"probe"`` fallback below, and if its parser
    rejects that value the field reads the same either way -- so a misfiled
    lift only fails here once its header has a parser-valid probe there.
    """
    # From ``_HEADER_PROBES``, not ``_CONTRACT_HEADER_LIFTS``: a misfiled lift
    # has no lift entry, and must still be sent a value its parser accepts.
    probes = dict(_HEADER_PROBES)
    # Declared-but-unpinned names (the X-Committed-Spend-* trio, nosniff) have
    # no field and so no normaliser to satisfy; any non-empty value will do,
    # and one that moves a field is the finding.
    probes |= {name: "probe" for name in _declared_run_response_headers() - probes.keys()}

    bare = _run_result({}, httpx.Headers())
    loaded = _run_result({}, httpx.Headers(probes))
    for field in sorted(_NON_HEADER_FIELDS):
        assert getattr(loaded, field) == getattr(bare, field), (
            f"RouterRunResult.{field} is listed in _NON_HEADER_FIELDS as not header-derived, "
            f"but it changed from {getattr(bare, field)!r} to {getattr(loaded, field)!r} when "
            f"the contract's 200 headers were supplied. It IS a lift: move it into "
            f"_CONTRACT_HEADER_LIFTS with the header it reads, so both pins apply to it."
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
