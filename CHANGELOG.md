# Changelog

All notable changes to `comfy-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for `v0.1.0` through `v0.1.8` were reconstructed from the published
[GitHub Releases](https://github.com/Comfy-Org/comfy-python-sdk/releases); those
release notes remain the fuller account, including the end-to-end verification
notes for each version.

## [Unreleased]

### Added

- `client.models.run(model, arguments)` on both `Comfy` and `AsyncComfy` — one
  call that returns the completed generation. Where the platform has to
  submit-and-poll an upstream provider, that polling happens server side inside
  the call, so the client contract stays a single request. The result is the
  provider's native payload, returned as-is rather than wrapped. The awaitable
  form is `AsyncComfy`, not a `run_async()` suffix — there is deliberately no
  suffixed variant, and a test asserts its absence. Runs use their own
  10-minute timeout (the client's default is sized for ordinary API calls) and
  send an `Idempotency-Key` on every call.

## [0.1.8] - 2026-08-13

### Added

- `Job.get_workflow()` / `AsyncJob.get_workflow()` — fetch the workflow behind a
  job, including one rehydrated by id. Returns the graph and a `format`
  discriminator: `save` (the authoring workflow at the version the job ran, with
  canvas layout and editor-only nodes intact) or `api` (the executed API-format
  graph). Jobs submitted through this SDK always get `api` today.
- Asset deletion — `Asset.delete()` and `assets.delete(id)`. Thanks to
  [@jab416171](https://github.com/jab416171) for the implementation. Requires
  backend support: Comfy Cloud has it; self-hosted needs a `comfy-api-proxy` new
  enough to serve `DELETE /api/v2/assets/{id}`, older proxies return
  `405 Method Not Allowed`.
- `job_id` on outputs and assets, so you can get from an output file back to the
  job that produced it without a side table. Absent for uploaded assets, which
  have no producing job.
- `expires_at` on assets.

### Fixed

- `job_id` and `expires_at` were present on the wire but not exposed by the
  public wrapper classes, so they were unreachable without touching a private
  attribute.

## [0.1.7] - 2026-08-12

There is no 0.1.6 on PyPI — that number was consumed by a release-pipeline
failure and never published.

### Changed

- **Breaking:** the base URL moves from a constructor argument to the
  `COMFY_BASE_URL` environment variable. `Comfy()` / `AsyncComfy()` target Comfy
  Cloud by default; point the client at another deployment by setting
  `COMFY_BASE_URL`. The variable is read on each construction (not at import),
  must be an `http(s)` URL, and unset-or-blank means Comfy Cloud.
- **Breaking:** `api_key` is keyword-only, so the old positional form raises
  `TypeError` rather than quietly reading a URL as a key.
- `comfy_low`, the documented escape hatch the clients are built on, still takes
  a base URL directly and is unchanged.

## [0.1.5] - 2026-07-30

Maintenance release. No API changes — existing code needs no updates.

### Fixed

- Ship `py.typed` (PEP 561), so type checkers in consuming projects actually see
  the SDK's type information. Previously the annotations were shipped but ignored.
- Derive `__version__` from installed distribution metadata instead of a
  hardcoded string, so it can no longer drift from the released version.

### Changed

- Ship an MIT license (the package previously declared none) and fill in the
  empty package metadata.
- Stop sweeping local dev droppings into the sdist — it now contains only what is
  needed to build and run the tests.
- The repository moved from `Comfy-Org/ComfyPythonSDK` to
  `Comfy-Org/comfy-python-sdk`. GitHub redirects the old URLs and the PyPI
  package name is unchanged (`comfy-sdk`). This is the first release to carry
  the corrected repository/issues URLs in its published metadata.
- Docstrings for the public methods that had none; README aligned with the
  TypeScript and Swift SDK READMEs.

## [0.1.4] - 2026-07-28

Comfy Cloud now serves the v2 API on `cloud.comfy.org`. `api.comfy.org`
continues to serve the node registry.

### Changed

- **Breaking:** `api.comfy.org/api/v2/*` no longer responds. If you pass that
  host explicitly, requests 404 until you update.
- `base_url` now defaults to `https://cloud.comfy.org`, so `Comfy(api_key=...)`
  targets Comfy Cloud with no host argument. `COMFY_CLOUD_BASE_URL` is exported
  for callers who want the value.
- Spec server URL, README, and docstrings updated to the new host.
- Passing an explicit `base_url` still wins — self-hosted and serverless callers
  are unaffected.

## [0.1.3] - 2026-07-27

### Fixed

- Serverless gateway: follow-up links no longer 404 after submit. A gateway
  serving the v2 API under a mount prefix (e.g. `/deployment/{id}/api/v2`)
  returns `job.urls.*` links that already include that prefix; resolving them
  against a `base_url` carrying the same prefix doubled it, so the first
  `Job.refresh()` after a successful submit raised `NotFound`. Server-returned
  links (leading slash, containing `/api/`) now resolve against the origin —
  the link is authoritative about its own path. Internal shorthand paths still
  resolve under `base_url`; Comfy Cloud and self-hosted behavior is unchanged.

### Added

- An env-gated live integration suite (`tests/integration/test_gateway_e2e.py`)
  covering upload → blake3 dedup fast path → img2img submit → poll → output
  download against a real gateway. Skipped unless `COMFY_BASE_URL` /
  `COMFY_API_KEY` are set.

## [0.1.2] - 2026-07-23

### Added

- `Output.get_download_url()` — get a fetchable URL for an output instead of
  streaming the bytes through your process. On Comfy Cloud / serverless it is a
  short-lived, self-authorizing signed storage URL (with `expires_at`); on a
  self-hosted proxy it is the content endpoint (`expires_at=None`). Available on
  both `Output` and `AsyncOutput`.
- The client now identifies itself via a `User-Agent` header; pass `client_info=`
  to attribute your own integration's traffic.

### Fixed

- SSE: a read-idle timeout, so a stalled stream can no longer hang `events()`.
- Map entity-specific 404s (`job_not_found` / `asset_not_found`) to `NotFound`.

## [0.1.1] - 2026-07-21

### Added

- Optional `api_key=` parameter on `submit()` / `run()` (sync and async) that
  authenticates partner (API) nodes in a workflow, sent as
  `extra_data.api_key_comfy_org`. Omit it (or pass `""`) and no `extra_data` is
  sent. The key is never logged or persisted and does not participate in
  idempotency.

## [0.1.0] - 2026-07-21

First public release of the Comfy API v2 Python SDK (`comfy-sdk`).

### Added

- Run ComfyUI workflows across self-hosted, Comfy Cloud, and serverless from one
  typed client: upload/dedup inputs, submit a workflow, follow it (poll or SSE),
  and download outputs.
- Sync and async clients. Python 3.10+.

[unreleased]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.8...HEAD
[0.1.8]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.5...v0.1.7
[0.1.5]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Comfy-Org/comfy-python-sdk/releases/tag/v0.1.0
