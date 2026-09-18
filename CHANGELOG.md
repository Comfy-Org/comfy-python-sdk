# Changelog

All notable changes to `comfy-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The [GitHub Releases](https://github.com/Comfy-Org/comfy-python-sdk/releases) carry
the fuller account of each version, including verification notes.

## [Unreleased]

### Fixed

- **`except RouterError` now catches every Comfy Router refusal.** `insufficient_credits`,
  `unauthorized` and `forbidden` raised a class that was *not* a `RouterError`, so the obvious
  catch-all around a `client.models.*` call caught nothing for them. Those three buckets are now
  one class each, exported from both `comfy_sdk.exceptions` and `comfy_sdk.router_exceptions` —
  `comfy_sdk.exceptions.InsufficientCredits is comfy_sdk.router_exceptions.InsufficientCredits`,
  so either import catches what the other does. A Router bucket this version does not know now
  raises `RouterError` rather than a bare `ComfyError`.
- A cancel the server refuses raises a named exception instead of an untyped `409`:
  `AlreadyCompleted` (the `{"status": "ALREADY_COMPLETED"}` answer to cancelling finished work),
  under the `CancelRefused` base. Nothing has to match on the response body text any more.
  Only the refusal shapes this version recognises are typed, and `ALREADY_COMPLETED` is the whole
  of that list today: a refusal that names no bucket and no code stays an untyped `ComfyError`,
  because nothing in such a response identifies it as a refusal at all.
- `models.run` now populates `RouterError.errors` from a Router 422's per-field `detail[]` and
  uses the entries' messages as `detail`, instead of `HTTP 422`; `comfy_low.ApiError.validation_errors`
  carries the raw entries.

### Changed

- **Because those three buckets are now one class each, they descend from `RouterError` on the
  workflow surface too**: a `POST /jobs` call that fails `401`/`403`/`402` raises a `RouterError`
  subclass. `except Unauthorized` / `except Forbidden` / `except InsufficientCredits` (from either
  module) and `except ComfyError` are unchanged; only `except RouterError` sees more than its name
  suggests.
- **Breaking, for code that *constructs* those three classes.** `Unauthorized`, `Forbidden` and
  `InsufficientCredits` are now `RouterError` subclasses, so they take `RouterError`'s
  constructor: the human-readable string is the positional `detail`, and the bucket is
  `error_type=`. There is no `message=` or `code=` keyword any more, so a hand-built
  `Unauthorized(message="...", code="unauthorized")` — in a test double, a re-raise, or a
  subclass — now raises `TypeError` and becomes `Unauthorized("...")`. Only construction is
  affected: `raise`, `except` and every attribute a caller reads inside the handler (`.message`,
  `.code`, `.http_status`, `.details`, `.request_id`, `.retry_after`) are unchanged.
- `RouterError` is exported from the package root, alongside `CancelRefused` and
  `AlreadyCompleted`. The fifteen per-bucket classes still live in `comfy_sdk.router_exceptions`.
- `ApiError.error_type` records the Router bucket a response named (`X-Comfy-Error-Type`, or the
  body's `error_type`), or `None` when it named none — which is also how the SDK tells which
  surface answered.

## [0.3.0] - 2026-09-14

### Added

- Queued model surface: `models.submit()`, `models.subscribe()`, `models.handle()`, and the
  `RequestHandle` they return (`status()`, `get()`, `cancel()`, `iter_events()`). Sync and async.
  Gated server side — a caller it is not enabled for gets `403` `NotEnabled`.
- `ComfyError.resend_refused` — `True` only on the failure re-raised after a refused same-key
  resend. Check it before letting an outer retry wrapper re-enter `run()`, which would mint a
  fresh key and bill a second generation.
- `retry.may_have_claimed_key(exc)` — whether a failure could have left the key claimed.
- `router_exceptions.error_from_completion()` — the typed exception a completed-but-failed queued
  request reports, or `None`.

### Changed

- **`models.run` now raises the failure that *claimed the key*** when a same-key retry is refused
  `422 idempotency_key_reuse`, with the refusal chained on `__cause__` and `.resend_refused` set.
  **`except IdempotencyKeyReuse` no longer catches this** — catch the real failure (or
  `ComfyError`) and inspect `__cause__`.
- The substitution only applies when a claim-capable failure preceded the refusal. After a
  never-delivered transport error or a released `429`, a `422` is a genuine refusal and is raised
  as itself. Among eligible failures the most recent wins, not the first.

### Fixed

- A `409` naming no error code raises plain `ComfyError` instead of guessing `HashMismatch`.
  Enveloped `hash_mismatch` and Router-bucketed `409`s are unaffected.

## [0.2.0] - 2026-09-10

### Added

- `Asset.get_download_url()` / `AsyncAsset.get_download_url()` — a fetchable URL for an uploaded
  asset, mirroring `Output.get_download_url()`. This is how a local image reaches a URL-taking
  image-to-image model.
- `ApiError.body_excerpt` — a bounded, sanitized excerpt of a response body that stated no message
  of its own, so a bare `HTTP 503: no healthy upstream` from a load balancer survives into logs.
  `None` when the response did state a message.

### Changed

- An unrecognised error response now carries code `http_<status>` instead of `"error"`. Reached
  only after the envelope code, Router bucket and status table all decline. Nothing about what is
  retried changed.

## [0.1.9] - 2026-09-01

### Added

- `models.run()` on `Comfy` and `AsyncComfy` — one call returning the completed generation, with
  server-side polling and the provider's native payload returned as-is. 10-minute timeout.
- Automatic retry for `models.run`, on by default, sending the same `Idempotency-Key` on every
  attempt of one logical call. Retried: connect-phase transport failures, and `429` with
  `Retry-After`. Not retried: other 4xx. Not retried without `retry_possibly_in_flight=True`:
  5xx and client-side timeouts. Budget is 60s total elapsed, backoff 0.5s→15s with jitter.
  Configure via `Comfy(retry=RetryPolicy(...))` or `NO_RETRY`.
- Every exception from a failed `models.run()` and `submit()` call carries `.idempotency_key`,
  so a lost response can be collected by passing it back. `submit()`'s key is the attempt's key,
  not a replay handle — `POST /jobs` rejects reuse rather than replaying.
- `ComfyError.request_id` (from `X-Comfy-Request-Id`) and `ComfyError.retry_after` on every
  exception, not just some.
- `MissingApiKey` raised locally at construction against Comfy Cloud instead of costing a `401`
  round trip. Credentials resolve `api_key=` then `COMFY_API_KEY`.
- `repr()` on `Comfy`/`AsyncComfy` reporting base URL and `authenticated=`. Keys are never
  rendered; a credential embedded in the base URL is redacted.

### Changed

- **`models.run` posts to Comfy Router**: `POST {COMFY_ROUTER_BASE_URL}/v2/models/{provider}/{model}`
  with the model's native JSON as the body. If you tracked `main` and pointed `COMFY_BASE_URL` at a
  Router host, point `COMFY_ROUTER_BASE_URL` there instead.
- **Breaking:** the `model` argument is now the canonical `{provider}/{model}` id. Exactly two
  non-empty segments; anything else raises `ValueError` locally. Matches the TypeScript SDK.
- `COMFY_ROUTER_BASE_URL` (default `https://api.comfy.org`) selects the Router deployment,
  separate from `COMFY_BASE_URL`. `models.base_url` reports it.
- The API key is attached to both configured origins and no third one.

### Fixed

- Router errors keep their own bucket on every status, so `403 not_enabled` raises `NotEnabled`
  rather than `Forbidden`. Previously the status table won and `except NotEnabled` never fired.
- A Router `409` keeps its contract bucket instead of decoding to `HashMismatch`;
  `concurrency_limit_exceeded` now drives the collect retry.
- An explicit `idempotency_key` is validated locally (1–255 printable ASCII). The empty string
  used to mint a fresh key, silently dispatching a second billed generation.
- A success status whose body will not decode raises a translated SDK error instead of letting
  `json.JSONDecodeError` escape.

## [0.1.8] - 2026-08-13

### Added

- `Job.get_workflow()` / `AsyncJob.get_workflow()` — the workflow behind a job, with a `save` or
  `api` format discriminator.
- Asset deletion: `Asset.delete()` and `assets.delete(id)`. Thanks to
  [@jab416171](https://github.com/jab416171). Needs a proxy serving `DELETE /api/v2/assets/{id}`.
- `job_id` on outputs and assets; `expires_at` on assets.

### Fixed

- `job_id` and `expires_at` were on the wire but unreachable from the public classes.

## [0.1.7] - 2026-08-12

There is no 0.1.6 on PyPI — that number was consumed by a release-pipeline failure.

### Changed

- **Breaking:** the base URL moves from a constructor argument to the `COMFY_BASE_URL` environment
  variable. Unset or blank means Comfy Cloud. Read per construction, must be `http(s)`.
- **Breaking:** `api_key` is keyword-only, so the old positional form raises `TypeError`.
- `comfy_low` still takes a base URL directly and is unchanged.

## [0.1.5] - 2026-07-30

Maintenance release. No API changes.

### Fixed

- Ship `py.typed` (PEP 561), so consumers actually see the SDK's types.
- Derive `__version__` from distribution metadata so it cannot drift.

### Changed

- Ship an MIT license and fill in the empty package metadata.
- Trim local dev droppings from the sdist.
- Repository moved to `Comfy-Org/comfy-python-sdk`. PyPI name unchanged.
- Docstrings for public methods; README aligned with the other SDKs.

## [0.1.4] - 2026-07-28

Comfy Cloud now serves the v2 API on `cloud.comfy.org`; `api.comfy.org` serves the node registry.

### Changed

- **Breaking:** `api.comfy.org/api/v2/*` no longer responds. Passing that host explicitly 404s.
- `base_url` defaults to `https://cloud.comfy.org`. An explicit `base_url` still wins.

## [0.1.3] - 2026-07-27

### Fixed

- Serverless gateway: follow-up links no longer 404 after submit. A gateway serving under a mount
  prefix returned links already carrying it, and resolving against `base_url` doubled the prefix.
  Server-returned links now resolve against the origin.

### Added

- Env-gated live integration suite covering upload → dedup → img2img → poll → download.

## [0.1.2] - 2026-07-23

### Added

- `Output.get_download_url()` — a fetchable URL instead of streaming bytes through your process.
- A `User-Agent` header; pass `client_info=` to attribute your own traffic.

### Fixed

- SSE read-idle timeout, so a stalled stream cannot hang `events()`.
- Map `job_not_found` / `asset_not_found` to `NotFound`.

## [0.1.1] - 2026-07-21

### Added

- Optional `api_key=` on `submit()` / `run()` authenticating partner (API) nodes, sent as
  `extra_data.api_key_comfy_org`. Never logged, and not part of idempotency.

## [0.1.0] - 2026-07-21

First public release of the Comfy API v2 Python SDK (`comfy-sdk`).

### Added

- Run ComfyUI workflows across self-hosted, Comfy Cloud and serverless from one typed client:
  upload/dedup inputs, submit, follow (poll or SSE), download outputs.
- Sync and async clients. Python 3.10+.

[unreleased]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.9...v0.2.0
[0.1.9]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.5...v0.1.7
[0.1.5]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Comfy-Org/comfy-python-sdk/releases/tag/v0.1.0
