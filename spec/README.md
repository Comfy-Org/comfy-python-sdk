# Vendored specs

Vendored copies of the canonical HTTP contracts this SDK is built against, synced one-way — do not hand-edit either file. The sync strips any operation tagged `internal` / `x-internal: true` (this is a public repo).

- **`openapi.yaml`** — the **Comfy API v2** contract (OpenAPI 3.0.3), pinned by `VERSION`. Regenerate `src/comfy_low/models/` from it with `scripts/gen_models.sh`; CI (`scripts/check_drift.py`) fails on drift.
- **`router-openapi.yaml`** — the **Comfy Router** public contract (OpenAPI 3.0.2): the model catalog and the model-ID-addressed invocation routes, with the error buckets they return. Nothing is generated from it today. What it does gate is the typed error surface: its `RouterErrorType.x-comfy-error-types` list is the closed error set, and `scripts/check_drift.py` fails unless `comfy_sdk.router_exceptions.ROUTER_ERROR_TYPES` is **exactly that list of values, in exactly that order** — so a bucket added, removed or reordered upstream all fail it, not only an addition. `tests/test_router_spec_contract.py` asserts the same thing from the suite.

**Syncing a new Router spec is therefore a two-step change**, and the drift check is what makes the second step unskippable. After dropping in the new `router-openapi.yaml`, reconcile `src/comfy_sdk/router_exceptions.py` against it:

- **A value was added** — add one `RouterError` subclass for it, named as the PascalCase of the wire value, carrying that entry's `meaning` as its docstring, positioned so `ROUTER_EXCEPTIONS` still follows the spec's declaration order.
- **A value was removed** — removing the class is a breaking change to anyone's `except` clause, so it is a decision to make deliberately rather than a mechanical edit. Whatever you decide, the two lists have to end up equal for the check to pass.
- **The order changed** — reorder `ROUTER_EXCEPTIONS` to match. `ROUTER_ERROR_TYPES` derives from it, and both SDKs present the set in the spec's order.
- **A `meaning` changed** — update that class's docstring. This is the one case **no check catches**: the gate compares wire values and order, not prose, because the docstrings deliberately reword the spec's `meaning` into reST rather than quoting it verbatim. Diff the `x-comfy-error-types` block by hand when reviewing a sync.
