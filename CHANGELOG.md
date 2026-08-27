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

- Every exception `client.models.run()` raises now carries the
  `Idempotency-Key` the call was made under, on `.idempotency_key` — the typed
  `RouterError` buckets, a `RouterError` whose `error_type` this version does
  not recognise, any other `ComfyError`, and a transport failure with no
  response at all (a dropped connection, a read timeout), and a cancelled
  `await` of `AsyncModels.run` — the `asyncio.wait_for` a caller wraps a
  ten-minute call in abandons a generation that may already be dispatched and
  billed, and the cancellation still propagates unchanged. `run` mints that key
  itself unless you pass `idempotency_key=`, and it used to be a local of the
  call: when the call raised, the key went with it. Since collecting a
  generation you were already billed for after a lost response means asking
  again under the *same* key, that made the auto-minted case uncollectable —
  only callers who chose and stored their own key could recover. The recovery
  idiom is now `client.models.run(model, arguments,
  idempotency_key=exc.idempotency_key)`; see the README. Nothing about what is
  retried, or what goes on the wire, changed.
- `ComfyError.request_id` — the server's `X-Comfy-Request-Id` for the failed
  call, when the response carried that header, on every SDK exception rather
  than only on `RouterError`. It is the id to quote in a support request, and it
  was previously unreachable once the response object was gone. `None` when the
  response named none, or when there was no response — including on a transport
  failure, where the attribute now reads as `None` rather than being absent, so
  a handler never has to guard the access. The header is bounded and filtered
  before it is stored (it is server-controlled and the id is meant to be
  displayed and pasted into support tickets), identically on both error
  surfaces.
- `ComfyError.retry_after` — seconds the server asked the caller to wait, from
  `Retry-After`, now forwarded for every error code rather than only for
  `queue_full`. The replay documented above tells a caller to ask again "after
  the `Retry-After` the server named", and a `deadline_exceeded` `504` that
  carried one had nowhere to surface it, so the caller had nothing to wait on.
  `None` when the server named no pace.

### Fixed

- A success status whose body will not decode (a proxy interstitial served
  under a `200`, a response truncated mid-stream) now raises a translated SDK
  error instead of letting `json.JSONDecodeError` escape from outside the
  translated surface. On `models.run` that is a generation that ran and was
  billed with the result lost — precisely the failure the `Idempotency-Key`
  has to ride out on, and it previously carried no key.
- Automatic retry for `client.models.run`, on by default, with the
  `Idempotency-Key` sent unconditionally on **every** attempt of one logical
  call — a new call mints a new key. That is what keeps a retry from being
  billed as a second generation, and it is also what decides which failures are
  retried at all: the key is single-use and reject-on-duplicate with no
  response replay, so only failures that leave it unclaimed are worth another
  attempt. Retried by default: connect-phase transport failures (the request
  never reached the server), and a `429` carrying `Retry-After` (a reject that
  started no work, so the key is released) — paced by the `Retry-After` the
  server sent rather than a blind backoff. Not retried: every other 4xx, since
  a deterministic refusal such as `content_policy_violation`, `404`, `409` or
  `422` cannot change on the second ask. Not retried unless
  `retry_possibly_in_flight=True`: anything whose outcome is unknown — a 5xx
  response, or a client-side timeout on a run the server may still be
  generating — because the key stays claimed across those and a same-key retry
  would come back `422 idempotency_key_reuse` in place of the real error. Turn
  the opt-in on for a deployment that replays a repeated key. The budget is 60
  seconds of **total elapsed time** from the first attempt rather than an
  attempt count, and it bounds when the last attempt may *start*: an attempt
  already running is never interrupted, so a slow generation is never abandoned
  half-way. Backoff is 0.5s doubling to a 15s ceiling with full jitter, clamped
  to whatever is left of the budget. Configure with
  `Comfy(retry=RetryPolicy(...))` or switch it off with
  `Comfy(retry=NO_RETRY)`; `client.models.retry` reads back the policy in
  force. `RetryPolicy`, `DEFAULT_RETRY` and `NO_RETRY` are exported from
  `comfy_sdk`.
- `client.models.run(model, arguments)` on both `Comfy` and `AsyncComfy` — one
  call that returns the completed generation. Where the platform has to
  submit-and-poll an upstream provider, that polling happens server side inside
  the call, so the client contract stays a single request. The result is the
  provider's native payload, returned as-is rather than wrapped. The awaitable
  form is `AsyncComfy`, not a `run_async()` suffix — there is deliberately no
  suffixed variant, and a test asserts its absence. Runs use their own
  10-minute timeout (the client's default is sized for ordinary API calls) and
  send an `Idempotency-Key` on every call.
- Credential resolution with a documented order: the explicit `api_key=`
  argument first, then the `COMFY_API_KEY` environment variable. Against Comfy
  Cloud, which always requires a key, neither raises the new `MissingApiKey`
  locally at construction — naming `COMFY_API_KEY` in the message — instead of
  costing a round trip to be told `401`. Both sources are trimmed, and a blank
  value counts as unset. Comfy Cloud is recognized by normalized origin (scheme,
  host, effective port) and path rather than by string, so a `COMFY_BASE_URL`
  that spells it differently — `https://cloud.comfy.org:443/`, or with a
  mixed-case host — gets the same local error rather than a server `401`.
- `MissingApiKey` (a `ComfyError`, `code="missing_api_key"`) and
  `API_KEY_ENV_VAR` are exported from `comfy_sdk`.
- `Comfy`/`AsyncComfy` now have an explicit `repr()` reporting the base URL and
  `authenticated=True|False`. The key is never rendered, logged, or included in
  an exception message. A credential embedded in the base URL itself
  (`COMFY_BASE_URL=https://user:token@proxy.example`, for a deployment behind an
  authenticating proxy) is redacted to `***@host` in every `repr()` — the
  client, its transport and its `models` namespace — while requests still go out
  against the URL exactly as given.

### Changed

- A client targeting a deployment named by `COMFY_BASE_URL` is unchanged: with
  no key resolved it is still built without one and still sends no credentials,
  which is what a self-hosted ComfyUI behind the API proxy needs. Only the Comfy
  Cloud default gained the local error.

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
