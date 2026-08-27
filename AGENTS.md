# AGENTS.md

Working notes for AI coding agents. Everything here is enforced by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) or by a script in
[`scripts/`](scripts/) — it is not style advice.

Read the two traps first. Both of them pass local review and fail CI.

---

## Trap 1 — do not hand-edit the generated models

`src/comfy_low/models/_generated.py` is **emitted by
[`scripts/gen_models.sh`](scripts/gen_models.sh) from
[`spec/openapi.yaml`](spec/openapi.yaml)**. It is committed to the tree, so it
looks like ordinary source, and nothing local will stop you editing it:

- ruff skips it — `extend-exclude = ["src/comfy_low/models/_generated.py"]`
- ruff format skips it for the same reason
- mypy lists it in `exclude`

So a hand-edit lints clean, formats clean, and the tests may well still pass.
The `codegen-drift` CI job then regenerates the file into a temp dir and
compares it **byte for byte** against the committed one
([`scripts/check_drift.py`](scripts/check_drift.py)). Any hand-edit fails there.

`spec/openapi.yaml` is *also* not yours to edit: it is a one-way vendored copy
of the canonical contract (see [`spec/README.md`](spec/README.md)). Editing the
spec to make drift pass is the wrong fix in the other direction.

### What is generated vs hand-written

| Path | Status |
|---|---|
| `src/comfy_low/models/_generated.py` | **Generated.** Never hand-edit. |
| `spec/openapi.yaml`, `spec/router-openapi.yaml`, `spec/VERSION` | **Vendored**, synced one-way. Never hand-edit. |
| `src/comfy_low/models/__init__.py` | Hand-written. Re-exports names from `_generated.py`; if a schema is added or renamed, update the import list and `__all__` here yourself — codegen does not touch this file. |
| everything else under `src/comfy_low/` | Hand-written (transport, SSE decoder, errors, multipart). |
| everything under `src/comfy_sdk/` | Hand-written. This is the public SDK surface. |

### Regenerate

```bash
uv run --extra codegen bash scripts/gen_models.sh   # rewrites src/comfy_low/models/_generated.py
uv run --extra codegen python scripts/check_drift.py  # the exact check CI runs
```

`check_drift.py` runs two checks, not one: the byte-for-byte model comparison
above, and the Router error-type comparison described under "Other things that
are not obvious" below. Both run on every invocation, so a failure in one does
not hide the other.

Then commit `src/comfy_low/models/_generated.py`.

Two things that bite here:

- `gen_models.sh` is a **bash** script, not Python. `python scripts/gen_models.sh`
  does not work.
- The generator version is load-bearing. `datamodel-code-generator` is pinned
  to `~=0.68.1` in `pyproject.toml` precisely because the drift check is a
  byte-for-byte comparison — a different generator version reformats the output
  and reports false drift. Do not bump that pin as a drive-by.

---

## Trap 2 — mypy gates every PR, and nothing in the repo advertises it

There is no `typecheck` script, no Makefile, no pre-commit config. The only
place the type check exists is a step in `ci.yml`. If you only run the tests,
you will push a red PR.

The four steps of the `test` job, in order, are the complete required set:

```bash
uv run --extra dev ruff check .          # lint
uv run --extra dev ruff format --check . # formatting (check only, does not write)
uv run --extra dev mypy src              # type check — NOT the whole repo, just src/
uv run --extra dev pytest -v             # tests
```

Notes on the invocations:

- `uv.lock` is committed, so `uv run` is the reliable local path. The dev tools
  live in the **`dev` extra**, not in a dependency group, so they are not part
  of the default sync set — without `--extra dev` the tools are not installed.
  (`uv run` re-syncs the environment each invocation, so pass `--extra dev`
  every time rather than relying on an earlier `uv sync`.)
- CI itself does not use uv. It runs `pip install -e .[dev]` and then the bare
  commands above. Either path must produce the same result.
- `mypy src` deliberately excludes `tests/`. Do not "fix" a PR by widening it.
- `ruff format --check` only reports. Use `ruff format .` to actually apply.

The `test` job runs on the full matrix `3.10`, `3.11`, `3.12`, `3.13`. The
floor is real: `requires-python = ">=3.10"`, and ruff/mypy are both configured
for `py310`. Anything newer than 3.10 syntax breaks a quarter of the matrix.

---

## The other two CI jobs

### `public-repo-hygiene`

This repo is public. [`scripts/check_public_repo_hygiene.py`](scripts/check_public_repo_hygiene.py)
scans every git-tracked file (except its own source and
`src/comfy_low/models/`) and fails on three categories:

1. **Ticket-shaped identifiers** — anything matching `[A-Z]{2,6}-\d{2,6}`.
   Common tech acronyms are handled by an explicit allowlist in the script.
2. **Internal collaboration-tool links/markers** — Notion, Slack archive and
   client links, Google Docs/Drive, Datadog, PostHog project links, Linear, and
   `incident-<n>` strings.
3. **References to a `Comfy-Org/<repo>` outside the known-public allowlist**,
   and `@Comfy-Org/<team>` handles outside the known-public team allowlist.
   This is default-deny: an unlisted name is flagged, not silently permitted.

Practical consequence: **do not paste internal context into code comments,
docstrings, commit-adjacent docs, or test fixtures.** Describe *why* in plain
prose instead of linking to where the discussion happened. If a flag is a
genuine false positive, extend the allowlist in the script with a comment
explaining why — do not loosen the regex.

Run it locally with plain Python; it needs no dependencies:

```bash
python3 scripts/check_public_repo_hygiene.py
```

### `build-check`

Builds a wheel and an sdist and runs `twine check dist/*` — a publish dry run,
so a broken package surfaces in PR CI rather than at release time. The sdist
contents are an **explicit** allowlist under
`[tool.hatch.build.targets.sdist]`; adding a new top-level directory that needs
to ship means adding it there, or it silently will not be packaged.

---

## Other things that are not obvious

- **Syncing `spec/router-openapi.yaml` is a two-file change.** Nothing is
  generated from the Router spec, but `scripts/check_drift.py` (and
  `tests/test_router_spec_contract.py`) compare its
  `RouterErrorType.x-comfy-error-types` list against
  `comfy_sdk.router_exceptions.ROUTER_ERROR_TYPES` — same values, same order,
  so an addition, a removal *and* a reorder each fail it. A sync therefore also
  needs the matching edit to the class table: one `RouterError` subclass per new
  value (PascalCase of the wire value, that entry's `meaning` as its docstring),
  the tuple reordered if the spec reordered, and a removal treated as the
  breaking change it is rather than a mechanical delete. Without it a new bucket
  reaches callers as an untyped `RouterError` — the drift this gate exists to
  catch. The one thing it does **not** catch is a changed `meaning`: the gate
  compares values and order, not prose, because the docstrings reword the spec
  rather than quoting it. `spec/README.md` has the full reconcile procedure.
- **Adding an operation to the contract is a four-file change.**
  `tests/test_spec_coverage.py` asserts that every non-internal `operationId`
  in `spec/openapi.yaml` appears in `comfy_low.OPERATION_IDS`, that
  `OPERATION_METHODS` covers exactly that set, and that both `ComfyLow` **and**
  `AsyncComfyLow` expose the mapped method. A spec sync therefore needs:
  regenerated models, updated `OPERATION_IDS` / `OPERATION_METHODS` in
  `src/comfy_low/__init__.py`, and a method on each transport.
- **Every public `def` in `comfy_sdk` must be annotated.** A mypy override sets
  `disallow_untyped_defs = true` for `comfy_sdk.*` only; the rest of the tree
  is looser. Both packages ship `py.typed`.
- **Deprecation warnings are errors.** `filterwarnings` promotes
  `DeprecationWarning` and `PendingDeprecationWarning` to failures, so a
  dependency bump can turn the suite red without any source change. That is
  intentional — it is the advance warning before a breaking upgrade.
- **The test suite has no network dependency.** `tests/conftest.py` runs a
  stdlib-only stub of the API in a background thread and points the SDK at it
  via `COMFY_BASE_URL`. Do not add mocks of `httpx`; drive `server.state`
  instead.
- **`tests/integration/test_gateway_e2e.py` is skipped** unless both
  `COMFY_BASE_URL` and `COMFY_API_KEY` are set. It is collected by default and
  skips silently, so a green local run does not mean it passed.
- **`pytest` runs in `asyncio_mode = "auto"`** with `--strict-markers` and
  `--strict-config`. An unregistered marker is an error, not a warning.
- **Do not hardcode a version.** `comfy_sdk.__version__` is resolved from
  installed distribution metadata; the `version` in `pyproject.toml` is a
  placeholder that the publish workflow overwrites with the release tag.
  `tests/test_version.py` guards this.
- **Every file needs an approving review from a code owner**
  (`.github/CODEOWNERS` covers `*`), so an agent cannot self-merge anything.

## Commit and PR conventions

- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`; `feat!:` for
  a breaking change) — the git history is uniform on this.
- **No AI attribution trailers.** Do not add `Co-Authored-By` lines or
  "generated with" footers to commits in this repo.
- CodeRabbit reviews every non-draft PR and is configured
  (`.coderabbit.yaml`) *not* to repeat ruff/mypy findings — so a CodeRabbit
  comment is about behavior, not style, and is worth reading closely.
