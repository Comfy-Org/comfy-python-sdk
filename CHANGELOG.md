# Changelog

All notable changes to `comfy-sdk` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The [GitHub Releases](https://github.com/Comfy-Org/comfy-python-sdk/releases) carry
the fuller account of each version, including verification notes.

## [Unreleased]

### Added

- `RateLimited` is exported from `comfy_sdk` and `comfy_sdk.exceptions` as a fourth bucket shared
  with the Router surface, beside `Unauthorized`, `Forbidden` and `InsufficientCredits`. It is the
  same class as `comfy_sdk.router_exceptions.RateLimited`, and a v2 job route's `rate_limited`
  envelope now maps to it explicitly; it carries `.retry_after`. The low layer gains
  `comfy_low.errors.RateLimited` (also on `comfy_low`) for the same code.

## [0.5.0] - 2026-10-10

### Added

- Job metadata. `submit(..., metadata={"client": "acme"})` on `Comfy` and `AsyncComfy` stores
  string labels on a job, sent as the `metadata` field of `POST /api/v2/jobs` (omitted when
  not given, so the request is unchanged). `Job.metadata` / `AsyncJob.metadata` read them back
  (`{}` when the job has none). `list_jobs(metadata=, limit=)` walks `GET /api/v2/jobs`, sends
  each filter as `metadata[<key>]=<value>`, follows `next_cursor` to the last page, and yields
  `JobSummary` items (`id`, `status`, `create_time`, `update_time`, `deployment_id`,
  `metadata`, `data`); on `AsyncComfy` it is an async iterator. The SDK leaves the label limits
  to the server: a refused map raises `ComfyError` with code `metadata_invalid` and the
  server's message (naming the key when one key or value breaks a rule, giving the count
  when there are more than 16 pairs), and a
  refused filter raises `ComfyError` with code `invalid_metadata_filter` (`invalid_cursor` for a
  cursor the server did not issue). A list item whose `id` or `status` is missing, null or not a string, or
  whose `deployment_id` is neither a string nor null, raises `ComfyError` with code
  `invalid_response` naming the field, as does an item that is not a JSON object (the item is
  not skipped), a page body that is not a JSON object, a page whose `jobs` is not an array (a
  missing or null `jobs` reads as an empty page), and a page whose `next_cursor` is not a string
  or repeats a cursor the same `list_jobs` iteration already sent (raised before that page is
  requested again, so the iteration ends instead of looping; a missing, null or empty
  `next_cursor` still ends the list). `list_jobs` also checks the filters on each item and skips
  one whose labels do not match (comparing each key and value as the text the query sends), so a host
  that ignores the filters yields only real matches (on one that pages, after reading as many
  pages as it takes).
  Each page retries a 429 that carries `Retry-After`, as `submit` does. A `metadata` that is not a map of strings reads as `{}` and a non-string value
  is dropped, on jobs and list items alike, instead of raising. Labels work on a deployment's
  address, and need a deployment gateway with job-label support; `list_jobs` there lists the
  deployment's jobs, and at the workspace address (`COMFY_BASE_URL=https://platformapi.comfy.org`, which serves
  the job list only) every job in the workspace. An older gateway accepts
  `metadata` on `submit` but does not keep it, and ignores the `list_jobs` filters, so a filtered
  `list_jobs` yields nothing there (the SDK's own filter check drops every job). Comfy Cloud refuses them for now: `submit` raises `ComfyError` code
  `metadata_not_supported` and `list_jobs` raises code `not_implemented` (HTTP 501). A
  self-hosted `comfy-api-proxy` does not keep labels: it refuses a label map with code
  `invalid_request`, its string `metadata` reads as `{}`, a filtered `list_jobs` yields
  nothing there, and an unfiltered one yields the proxy's newest jobs (50 by default, up to
  100 with `limit`) and stops, since the proxy sends no next cursor. Printing a `JobSummary`
  leaves out `data`, the raw item.
- `models.list()` and `models.schema()`, so you can discover Comfy Router models from Python
  as the TypeScript SDK already can. `list(cursor=, limit=, timeout=)` returns an iterable that
  walks the catalog (`GET /v2/models`), following `next_cursor` while `has_more` is true, and
  yields `CatalogModel` entries (`id`, `provider`, `model`, `billing`). `list(...).page()`
  returns one `ModelPage` (`data`, `has_more`, `next_cursor`, `limit`, `request_id`).
  `schema(model, etag=, timeout=)` reads `GET /v2/models/{provider}/{model}/openapi.json` into a
  `SchemaResult`. With `etag=`, it sends `If-None-Match`, and a `304` returns `unchanged=True`
  with `document=None` rather than raising. Both methods use the Router host and the client's
  credential, raise the same typed Router exceptions as `models.run`, retry under the client's
  policy (a keyless read also retries a `5xx` or read timeout whenever that policy retries at
  all), and default to a 30-second timeout. `AsyncComfy` has the same methods
  (`async for ... in client.models.list()`, `await client.models.schema(...)`).
- `RouterRunResult.credits_used` — what Comfy Router reported a run cost, lifted from the
  `X-Comfy-Credits-Used` response header onto what `models.run_detailed()` returns. It is a
  price rather than a settled ledger entry, absent means "not reported" and never "free", and
  `0` is a real reported cost — so branch on `credits_used is not None`, not on truthiness.
  Carried as the wire string; binary `float` is the wrong type to reconcile money against.
  A value that is not a finite decimal — an empty header, a repeated one (`httpx` joins those
  with `", "`), `NaN`/`Infinity` — reports as `None` rather than passing through to break the
  `Decimal()` parse the field documents. The field defaults to `None`, so this stays additive
  for anything that constructs a `RouterRunResult` by hand.
  The vendored Router spec now declares `X-Comfy-Credits-Used` on the run route's `200`, so
  `tests/test_router_spec_contract.py` pins this lift against the contract like the other four.
- `QueueBacklogFull` in `comfy_sdk.router_exceptions`, for the Router bucket `queue_backlog_full`:
  a queued `submit` refused `429` because the caller already has too many requests waiting. It is
  not `ConcurrencyLimitExceeded` — the queue parks a submit at the in-flight limit, and this is the
  separate bound on how many may be left waiting. It clears as the caller's own queued requests
  finish. Before this, the bucket arrived as a bare `RouterError`.
- `BinaryResult` — importable from `comfy_sdk` — the second shape
  `models.run()` can return. `run` now branches on the response
  `Content-Type`, exactly as the run route's published `200` says a client
  must: `application/json` (or a `+json` suffix type) decodes to a `dict`
  exactly as before, and anything else comes back as
  `BinaryResult(content, content_type, request_id)`. The bytes are the
  partner's file verbatim — not base64-encoded, not wrapped in a dict, not
  decoded or transcoded — so `Path("out.mp3").write_bytes(result.content)` is
  the whole of it. `content_type` is the header including its parameters,
  because for some partner media types the parameters are part of what the
  bytes are (`audio/L16; rate=16000`); it is bounded and stripped of
  unprintable characters first, which no real media type contains, the way
  every other server-supplied string this SDK surfaces already is. The return
  annotation is therefore `dict[str, Any] | BinaryResult`; a caller that only
  uses JSON models sees no behaviour change, but a type checker will now ask
  them to narrow. `run_detailed` is the same story one level out:
  `RouterRunResult.output` carries whichever of the two shapes the run
  answered with.

  Two boundaries worth knowing: a 2xx whose `Content-Type` claims JSON and
  whose body will not parse still raises `invalid_response` (there the response
  promised a document and did not deliver one), while a 2xx that names *no*
  `Content-Type` is a `BinaryResult` unless its body is empty (`{}`, as on
  every other operation) or parses as a JSON **object**. Object, not merely
  valid JSON: that branch probes bytes nothing declared, so `null`, `[...]` and
  a bare number are bytes — accepting one would return a value outside the
  declared union, and a short binary body of all-ASCII digits parses as a
  number. The binary path runs inside the same translation as the JSON one, so
  a failure still carries `.idempotency_key` and an `Idempotent-Replayed`
  binary 200 comes back like a first run.

  The one deliberate behaviour change beyond no longer discarding binary
  generations: a non-JSON 2xx used to be read as "a proxy interstitial served
  as 200" and raised. On this route that
  reading is no longer available — the SDK cannot tell an interstitial from a
  partner's native text output, and the contract says the body is the
  partner's — so a `text/html` 200 now reaches the caller as bytes they can
  inspect, rather than discarding a generation they were billed for. Every
  other operation keeps the old reading, because JSON is the only success media
  type their routes declare. The same asymmetry decides the empty case: a
  declared-binary 200 with a zero-length body is a `BinaryResult` holding no
  bytes rather than an exception. Two checks tell an answer from an artefact —
  `request_id is None` means no Router answer was seen at all (the header is
  required on every one Router sends), and `not content` means nothing was
  delivered.

  The queued surface gets the identical branch: `RequestHandle.get()` /
  `AsyncRequestHandle.get()` and `models.subscribe()` now return
  `dict[str, Any] | BinaryResult` too, because the result route they collect
  from (`GET .../requests/{request_id}`) declares the same `application/json` /
  `*/*` pair `models.run()` does. Before this it still went through the
  JSON-only decoder, so a binary generation submitted through `submit()` raised
  `invalid_response` on collection even though the identical model run directly
  through `run()` already worked.

### Fixed

- **`RouterRunResult.replayed` was always `False` against a real deployment.** It was lifted
  from `X-Comfy-Idempotent-Replayed`; the header Comfy Router actually sends — and the only
  spelling `spec/router-openapi.yaml` declares, on the `200` as on the `400`/`409`/`422` — is
  `Idempotent-Replayed`, with no `X-Comfy-` prefix. A replayed, unbilled response was reported
  as a fresh generation. The prefixed spelling is *not* honoured as an alias, because Router
  does not send it. `tests/test_router_spec_contract.py` now pins every header `run_detailed`
  lifts against the name the vendored contract declares, and pins that the lift reads it.
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
- **A Router `detail[]` summary now names its fields, and is sanitised.** Both the awaited
  (`models.run`) and the queued (`submit`) paths build the human-readable string with one shared
  function, so a single server response reads the same way whichever surface raised it. Each entry
  renders as `<loc>: <msg>`, so two `field required` errors now read `body.seed: field required;
  body.steps: field required` rather than collapsing to an unrecoverable `field required; field
  required`. The joined line gets the same treatment every other body-derived string already gets:
  control characters, ANSI escapes and bidi overrides reduced, whitespace collapsed, and a 256-character
  cap — so a hostile or merely careless `msg` can no longer scribble on a terminal or flood a log line.
  Only the summary string changes; `.errors` still carries the raw typed entries.
  An entry that is nothing but control characters no longer costs the summary its
  readable entries: it reduces to nothing, so it is skipped rather than charged against
  the length budget, where before a single such entry could exhaust the budget on its own
  and leave the caller with a bare `HTTP 422` while the fields that actually failed sat
  unread in the same body.
- **The string `detail` and `error.message` forms are sanitised too.** A Router request-level
  `detail` string, and a v2 envelope's `error.message`, reached `str(exc)` exactly as sent — so a
  server, a proxy or a provider in front of either surface could put ANSI escape sequences, a bidi
  override, NUL bytes, newlines or ten thousand characters of padding straight into a traceback or a
  log line. Both now get the same reduction the `detail[]` summary and the body excerpts already got:
  control characters and format characters replaced, whitespace collapsed to one line, and a
  256-character cap. It applies on every builder — the awaited `models.run` path and both queued
  paths. One behaviour change falls out of it: a `detail` of nothing but whitespace now reads as
  absent, so the error reports `HTTP <status>` (or, on a completion, the `error_type`) instead of a
  blank description. `RouterError.errors` and `ApiError.validation_errors` are untouched — those are
  data, and stay raw.
- `client.models.run()` no longer throws away a generation whose model answers
  in bytes rather than JSON. Comfy Router forwards a partner model's output
  under the partner's *own* media type, and for a model whose partner returns a
  generation directly as a file — the ElevenLabs audio models are the first of
  these in the catalog — that is raw `audio/mpeg`. The SDK called `.json()` on
  every 2xx regardless, so such a run raised `ComfyError` with
  `code="invalid_response"` (`UnicodeDecodeError: 'utf-8' codec can't decode
  byte 0xff`, the MP3 frame sync) *after* the generation had run and been
  billed. Those models were unusable from this SDK.

### Changed

- `submit` (and the new `list_jobs`) wait at least one second before retrying a 429, so a
  `Retry-After: 0` or a negative one no longer retries at once.
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
  `AlreadyCompleted`. The nineteen per-bucket classes still live in
  `comfy_sdk.router_exceptions`.
- `NotEnabled`'s documented meaning widened with the synced Router spec: on a queued `submit` it
  can also refuse a *model* whose partner answers a generation directly as bytes (it cannot yet be
  queued; nothing is queued or charged; `models.run` serves it). It is still terminal, but on a
  submit it no longer proves the caller is not switched on — read `.detail`.
- **Breaking, for direct `comfy_low` callers.** The body half of the `(body, headers)` tuple
  that `ComfyLow.post_model_run` / `AsyncComfyLow.post_model_run` and
  `get_model_request_result` return is now `dict[str, Any] | BinaryResult` (also importable
  from `comfy_low`), so code that indexes it must narrow first: a binary 200 that raised
  `invalid_response` in 0.4.0 now returns a `BinaryResult`. `comfy_sdk` users are covered by
  the `BinaryResult` entry above.
- `ApiError.error_type` records the Router bucket a response named (`X-Comfy-Error-Type`, or the
  body's `error_type`), or `None` when it named none — which is also how the SDK tells which
  surface answered.

## [0.4.0] - 2026-09-18

### Added

- `model_provider`, `strict_mode` and `fallback_provider` on `models.run` / `AsyncModels.run` —
  sent only when set, so a call that names none is byte-for-byte the request this route always
  made. `fallback_provider` accepts a `bool`, sent as `true`/`false`.
- `run_detailed()` (sync and async), returning a new `RouterRunResult`: the partner's native
  `output` plus `serving_provider`, `dropped_params`, `replayed` and `request_id`. `run()` is
  unchanged and still returns the native body. `RouterRunResult` is exported from `comfy_sdk`.
- `Cancelled`, `QueueTimeout` and `RequestNotFound` in `comfy_sdk.router_exceptions`, for the
  Router buckets `cancelled`, `queue_timeout` and `request_not_found`.

### Changed

- **Breaking, for direct `comfy_low` callers.** `ComfyLow.post_model_run` now returns
  `(body, headers)`, matching the four `*_model_request*` queue methods beside it. `comfy_sdk`
  users are unaffected.

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

[unreleased]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Comfy-Org/comfy-python-sdk/compare/v0.3.0...v0.4.0
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
