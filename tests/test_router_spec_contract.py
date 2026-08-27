"""The router binding is the vendored contract's, not this repo's.

Two things are read straight out of ``spec/router-openapi.yaml`` rather than
restated here:

* the closed error set it declares as ``x-comfy-error-types`` -- one entry per
  wire value, each with the ``meaning`` prose the exception docstrings
  reproduce -- compared against :mod:`comfy_sdk.router_exceptions`;
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
from comfy_sdk.router_exceptions import (
    ROUTER_ERROR_TYPES,
    ROUTER_EXCEPTIONS,
    RouterError,
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
