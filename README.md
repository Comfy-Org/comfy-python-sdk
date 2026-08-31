<div align="center">
<img src="assets/logo.svg" alt="Comfy" width="130"/>
<h1>comfy-python-sdk</h1>
<p>
  <strong>The Python client for the <a href="https://docs.comfy.org">Comfy API v2</a>.</strong><br/>
  Submit a workflow, stream its progress, get your outputs — against self-hosted ComfyUI, Comfy Cloud, or serverless.
</p>
</div>

<p align="center">
  <a href="https://pypi.org/project/comfy-sdk/"><img src="https://img.shields.io/pypi/v/comfy-sdk?style=for-the-badge&logo=pypi&logoColor=white&label=PyPI" alt="PyPI"></a>
  <a href="#requirements-and-install"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-lightgrey?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://cloud.comfy.org"><img src="https://img.shields.io/badge/Comfy_Cloud-cloud.comfy.org-211927?style=for-the-badge" alt="Comfy Cloud"></a>
</p>

---

Python SDK for running ComfyUI workflows via the **Comfy API v2**. The same
code runs against Comfy Cloud, a serverless deployment, or a self-hosted
ComfyUI instance — only the `COMFY_BASE_URL` environment variable and an
optional API key change.

## Requirements and install

Requires **Python 3.10+**. Dependencies: `httpx`, `blake3`, `pydantic` (v2).

```bash
pip install comfy-sdk
```

To install from source instead (for local development, or to track an
unreleased commit):

```bash
git clone https://github.com/Comfy-Org/comfy-python-sdk
cd comfy-python-sdk
pip install -e .

# To install everything needed to lint/type-check/test locally
pip install -e ".[dev]"
```

### Optional dependencies

Install the optional `pil` extra with `pip install -e ".[pil]"` to use `Preview.to_pil()` for decoding an in-progress output preview to a `PIL.Image`.

### For local

The SDK works against a ComfyUI instance with **Comfy API v2**. Comfy Cloud and serverless instances deployed from our developer platform already use Comfy API v2. For local or self-hosted instances, **Comfy API v2** can be setup using the [comfy-api-proxy](https://github.com/Comfy-Org/comfy-api-proxy).

## Getting started

```python
from comfy_sdk import Comfy

client = Comfy(api_key="comfyui-...")   # Comfy Cloud

wf = client.workflows.from_file("workflow_api.json")

# Input assets are hashed locally with blake3
# If the server already has an identical copy we reuse it, if not we upload the asset
# The workflow is updated with the core/ASSET reference instead of a local file path
asset = client.assets.from_file("photo.png")
wf.set_input("10", "image", asset)

# Run workflow
# Get outputs using the output node Id as a reference
job = client.run(wf)
for output in job.get_outputs("9"):
    output.to_file(output.name)
```

### Constructing a workflow

`client.workflows` builds a `Workflow` from wherever your API-format graph already lives. All three constructors are local — none of them touches the network:

| Constructor | Takes |
|---|---|
| `from_file(path)` | a path to a `workflow_api.json` on disk |
| `from_json(graph)` | a graph already in memory, as a `dict` |
| `from_str(text)` | the JSON text of a graph |

Callers that assemble the graph in code — a service or a cron with no JSON file to ship alongside it — want `from_json`:

```python
graph = {                                   # API format, same shape as workflow_api.json (abridged)
    "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
}

wf = client.workflows.from_json(graph)      # or from_str(json.dumps(graph))
wf.set_input("3", "seed", 42)               # sugar for graph["3"]["inputs"]["seed"] = 42

job = client.run(wf)
data = job.get_outputs("9")[0].to_bytes()   # bytes in memory, no file written
```

`from_json` wraps the dict you hand it rather than copying, and `wf.json` *is* that graph — still a plain, freely-mutable `dict` if you'd rather edit it directly than go through `set_input`. `AsyncComfy` exposes the same three constructors on `client.workflows`.

## Authentication — one client, per-surface key

| Surface | `api_key` |
|---|---|
| Comfy Cloud (`https://cloud.comfy.org`) — the default | Required |
| Serverless deployment | Required |
| Self-hosted ComfyUI (behind the API proxy) | Omit — no key is sent, even implicitly |

```python
client = Comfy(api_key="comfyui-...")   # Comfy Cloud
```

`AsyncComfy` takes the same arguments. A key is only ever attached to requests
aimed at the target deployment's own origin — a server-returned follow-up link
(`job.urls.self`/`cancel`/`events`, or a redirected asset download) pointing
anywhere else never receives it.

### Where the key comes from

Each client resolves its credential once, at construction, in a fixed order:

1. the explicit **`api_key=` argument** — it always wins;
2. the **`COMFY_API_KEY` environment variable**, when no argument was passed;
3. neither → **`MissingApiKey`**, raised locally against Comfy Cloud, which
   always requires a key. Nothing is sent, so you find out from the constructor
   rather than from a `401` on your first call.

```bash
export COMFY_API_KEY="comfyui-..."
```

```python
client = Comfy()                        # uses COMFY_API_KEY
client = Comfy(api_key="comfyui-...")   # this key, whatever the environment says
```

Both sources are read fresh on every construction (so a process can build
successive clients under different credentials), and both are trimmed — leading
and trailing whitespace is stripped, and a blank value counts as *unset*, so
`COMFY_API_KEY=` in a shell profile and a key read from a file with a trailing
newline both do the obvious thing.

Step 3 applies only to Comfy Cloud, recognized by its normalized origin and
path rather than by the exact string — `https://cloud.comfy.org:443/` is the
same deployment and gets the same local error. A deployment named by `COMFY_BASE_URL` may
have no auth at all, so if nothing resolves there the client is built without a
credential and sends none — the self-hosted row of the table above is unchanged.
A serverless deployment does require a key, and picks up `COMFY_API_KEY` the
same way; supply one or the server will answer `401`.

The key is write-only from the outside: it is never logged, never rendered by a
client's `repr()`/`str()` (they report `authenticated=True|False`, never the
key), and never placed in an exception message. The same holds for a credential
carried in the base URL itself — `COMFY_BASE_URL=https://user:token@proxy.example`
reaches a deployment behind an authenticating proxy, and every `repr()` renders
it as `https://***@proxy.example` while requests still use the URL as given.

`MissingApiKey` is a `ComfyError` like every other SDK exception, and is
distinct from `Unauthorized` — no key at all, versus a key the server rejected:

```python
from comfy_sdk import Comfy, MissingApiKey

try:
    client = Comfy()
except MissingApiKey as exc:
    print(exc)   # names COMFY_API_KEY and the api_key= argument
```

The low-level `comfy_low.ComfyLow` transport is unaffected: it takes the key it
is handed and reads no environment, since resolution is a `comfy_sdk` concern.

### Targeting another deployment

`Comfy()` points at Comfy Cloud and takes no base-URL argument. To run against
a serverless deployment or a self-hosted instance behind
[comfy-api-proxy](https://github.com/Comfy-Org/comfy-api-proxy), set
`COMFY_BASE_URL` in the environment:

```bash
export COMFY_BASE_URL="https://<deployment>.run.comfy.app"  # serverless
export COMFY_BASE_URL="http://127.0.0.1:8189"               # self-hosted proxy
```

It is read each time a client is constructed, must be an `http(s)` URL, and an
unset or blank value (including whitespace-only) means Comfy Cloud.

`COMFY_BASE_URL` selects the **jobs and assets** surface. `client.models` talks
to a different one — Comfy Router, `https://api.comfy.org` — and follows its own
variable, `COMFY_ROUTER_BASE_URL`, with the same rules (read per construction,
`http(s)` only, blank means the default):

```bash
export COMFY_ROUTER_BASE_URL="https://api.comfy.org"  # the default; set it to redirect model runs
```

Two variables rather than one because they are genuinely two hosts: the v2
surface serves `/api/v2/jobs` and `/api/v2/assets`, Router serves
`/v2/models/{provider}/{model}`, and neither serves the other's routes. Your
`COMFY_API_KEY` is the credential for both — the SDK attaches it to those two
configured origins and to no third one.

Upgrading from an earlier version: `Comfy("<url>", "<key>")` becomes
`Comfy(api_key="<key>")` with `COMFY_BASE_URL` set. `api_key` is keyword-only,
so the old positional call raises `TypeError` rather than reading a URL as a
key.

The SDK identifies itself via a `User-Agent` header (for support and usage
analytics) — this is request metadata only; no other data is collected. Pass
`client_info="my-app"` to append an `app/my-app` token so an integration can
attribute its own traffic:

```python
client = Comfy(api_key="comfyui-...", client_info="my-app")
```

## Partner (API) node auth

Workflows that use partner/API nodes (Gemini, etc.) need a Comfy API key to
authenticate them. Pass it per submit with `api_key=`. This is **not** the same
as the `api_key` you construct `Comfy` with: the constructor key authenticates
*you* to the server, while this one authenticates the partner nodes *inside* the
workflow (it is often the same `comfyui-…` key):

```python
job = client.run(wf, api_key="comfyui-...")
# or drive it yourself:
job = client.submit(wf, api_key="comfyui-...")
```

The SDK sends it once as `extra_data.api_key_comfy_org` alongside the workflow —
one key authenticates every partner node in the graph. It is never logged or
persisted by the SDK. Omit `api_key` and no `extra_data` is sent at all.

## Assets and `core/ASSET`

`client.assets.from_file(...)` / `from_bytes(...)` / `from_stream(...)` /
`from_url(...)` return a **lazy** asset handle immediately — no network call
yet. Embed it directly into the workflow graph:

```python
asset = client.assets.from_file("photo.png")
wf.set_input("10", "image", asset)
```

On first use (submitting the workflow, or an explicit `asset.commit()`), the
SDK:

1. hashes the bytes locally with blake3;
2. probes the server's dedup fast-path — a `HEAD` existence check by hash,
   then a cheap `from-hash` mint if the server already has those bytes;
3. only streams a full multipart upload on a miss.

At submit time, every asset handle found anywhere in the graph is replaced by
a `core/ASSET` reference object (`{"__type": "core/ASSET", "info": {"id":
..., "hash": ..., "file_path": ...}}`), which the server resolves back to the
uploaded asset when it runs the workflow.

Once committed, an asset also carries `job_id` — the id of the job that
produced it, or `None` for an asset with no producing job (e.g. a plain
upload) — and `expires_at`, its retention deadline, or `None` if it doesn't
expire.

Delete an asset with `asset.delete()`, or by id alone with
`client.assets.delete(asset_id)`:

```python
asset.delete()
# or, without holding a handle:
client.assets.delete(asset_id)
```

Deletion needs a proxy new enough to serve `DELETE /api/v2/assets/{id}` — an
older [comfy-api-proxy](https://github.com/Comfy-Org/comfy-api-proxy)
returns `405` instead. (`AsyncAsset.delete()` / `AsyncAssetFactory.delete()`
mirror both with `await`.)

## Live progress

```python
job = client.submit(wf)
for event in job.events():          # SSE; live, auto-reconnecting (no replay)
    match event:
        case Progress() as p:       print(f"{p.value:.0%} {p.message}")
        case Preview() as pv:       show(pv.to_pil())
        case OutputReady() as o:    o.output.to_file(f"partial/{o.output.name}")
        case StatusChange(status="succeeded"): break
result = job.result()               # raises JobFailed with node details on failure
```

`job.events()` reconnects automatically if the stream drops, but never
replays a frame you've already seen (the stream carries no cursor). That's
why polling stays authoritative: `job.wait()` / `job.result()` (and
`client.run()`, which is `submit()` + `result()`) always fall back to
`GET /jobs/{id}` to decide when a job is really done — use `events()` for
live UI feedback, and `wait()`/`result()`/`run()` for the definitive answer.
`job.status` is the current status string; `job.outputs` is the full list of
output handles regardless of which node produced them (`job.get_outputs(node_id)`
filters to one node, as in the quickstart above).

## Getting a job's workflow back

The SDK only holds the workflow it submitted for as long as the originating
`Job` handle stays alive — for a job rehydrated purely by id
(`client.jobs.get(job_id)`), `get_workflow()` is the only way to see the
graph:

```python
job = client.jobs.get(job_id)
wf = job.get_workflow()
match wf.format:
    case "api":   ...   # the executed graph; frontend-only nodes already resolved away
    case "save":  ...   # the authoring workflow at the pinned version, canvas layout intact
```

`format` discriminates the shape of `wf.graph`, so branch on it rather than
assume one. It depends on how the job was submitted, not on anything a caller
controls — jobs submitted through this SDK always get `"api"` today, since v2
submission has no version-pinning fields yet. (`AsyncJob.get_workflow()`
mirrors this with `await`.)

## Downloading outputs

A finished job exposes its results as `Output` handles — `job.outputs`, or
`job.get_outputs(node_id)` to filter to one node. Each output is an asset you
can pull down whichever way suits the caller:

```python
out = job.get_outputs("13")[0]
out.to_file("result.png")                   # stream to disk in chunks
with open("result.bin", "wb") as stream:
    written = out.to_stream(stream)          # write to an already-open binary stream
data = out.to_bytes()                       # buffer into memory
out.to_file("head.png", range=(0, 1023))    # range-aware: first 1 KiB only
```

Every output also carries `job_id`, the id of the job that produced it — so a
caller holding just an output can get back to the job that made it.

`get_download_url()` hands back a fetchable URL instead of transferring the
bytes through your process — give it to a browser, a CDN, or another service:

```python
link = out.get_download_url()               # DownloadUrl(url=..., expires_at=...)
```

On Comfy Cloud / serverless the URL is a short-lived, **self-authorizing**
signed storage URL: whoever holds it can read the asset until `expires_at`
with no API key of their own. On a self-hosted proxy it's the content endpoint
(normal auth still applies) and `expires_at` is `None`. It works on every
backend and never downloads the bytes first.

### Outputs are kind-typed

Outputs aren't assumed to be images. `output.type` is the normalized kind of what the node produced — one of `image`, `video`, `audio`, `text`, `file`, `latent` — and sits alongside `output.content_type` (the exact MIME type), `output.name` and `output.size_bytes`. Branch on it rather than sniffing the filename:

```python
for out in job.outputs:
    match out.type:
        case "image":
            out.to_file(out.name)                        # stream straight to disk
        case "audio":
            transcode(out.to_bytes(), out.content_type)  # bytes in memory, nothing written
        case "video":
            enqueue(out.get_download_url().url)          # hand the URL off, transfer nothing
        case _:
            print(out.type, out.name, out.size_bytes)
```

(`AsyncOutput` mirrors all of the above with `await`.)

## The `models` namespace

Model operations live in a namespace on the client you already constructed —
`client.models` — rather than in a second client object:

```python
client = Comfy(api_key="comfyui-...")

client.models.base_url   # Comfy Router's base URL, where model runs go
client.models.timeout    # the client's own HTTP timeout
```

The namespace is bound to that client's transport, so it uses the client's
credentials, connection pool and timeout, and a configuration change made on
the client afterwards applies through `models` as well — there is no second set
of settings to keep in sync. `AsyncComfy` carries the same `models` namespace,
and nothing extra is imported or constructed for it:
`from comfy_sdk import Comfy` stays the only entry point.

The one setting it does **not** share is the target host: `client.models.base_url`
reports `COMFY_ROUTER_BASE_URL` (`https://api.comfy.org` by default), not the
client's `COMFY_BASE_URL`. See
[Targeting another deployment](#targeting-another-deployment) for why they are
two variables.

`base_url` and `timeout` are a read-only view of that configuration; model
operations are added to this namespace as they land.

### `models.run` — one call, one result

```python
result = client.models.run("fal-ai/flux-pro", {"prompt": "a cat", "steps": 4})
result["images"][0]["url"]
```

That call is `POST https://api.comfy.org/v2/models/fal-ai/flux-pro` with
`{"prompt": "a cat", "steps": 4}` as the body.

Three things follow from that, and they are the whole contract of this method:

- **It targets Comfy Router** — `https://api.comfy.org`, redirected by
  `COMFY_ROUTER_BASE_URL`, not by `COMFY_BASE_URL`. Your API key goes with it.
- **The first argument is the model's canonical id**, `{provider}/{model}` —
  exactly the two segments that address the route, and exactly what Router's
  model catalog lists. It must be two non-empty segments: `"fal-ai"` alone,
  `"fal-ai/flux-pro/fp8"` (the three-segment variant form, which this route does
  not take yet), and anything containing a `.` or `..` segment all raise
  `ValueError` locally, before any request. A non-string raises `TypeError`.
- **The second argument is the model's own native input**, forwarded to the
  provider unchanged. There is no Comfy-shaped envelope around it: whatever the
  partner's own API documents as the request body is what you pass here, so you
  can move between the partner's API and Router by changing the host.

`run` returns when the generation is **complete**. There is no submit step and
nothing to poll: where the platform has to submit-and-poll an upstream
provider, that happens server side inside this one call. The value you get back
is the provider's own payload — decoded JSON, handed over as-is, with no
wrapper class between you and the fields the provider documented.

The awaitable form is the **async client**, not a differently-named method:

```python
async with AsyncComfy(api_key="comfyui-...") as client:
    result = await client.models.run("fal-ai/flux-pro", {"prompt": "a cat"})
```

There is no `run_async()`, and there will not be one — one operation, one name,
and `await` is what makes it asynchronous.

Because the server may legitimately hold the connection for minutes, `run` uses
its own 10-minute timeout rather than the client's (which is sized for ordinary
API calls). Pass `timeout=` seconds, an `httpx.Timeout`, or `None` to wait
indefinitely. Each call also sends a fresh `Idempotency-Key`, so an accidental
exact resend is rejected by the server instead of billing a second generation;
pass `idempotency_key=` to choose the value yourself.

### Retrying a run without paying for it twice

A failed `models.run` is retried automatically. **Every attempt of one call
sends the same `Idempotency-Key`**, and a new call mints a new one — that is
what lets a server tell a retry apart from a second order, on a surface where
one call is a billed generation.

That one key is also what decides *which* failures are worth retrying. The v2
jobs contract makes its `Idempotency-Key` **single-use, reject-on-duplicate,
with no response replay**: the first request to present a key is processed, and
a later one presenting the same key is rejected `422 idempotency_key_reuse`
rather than re-run. (That rule governs `submit()`. On the router surface a
resend of the same key with the same body *collects* the generation the first
request started instead of dispatching another; a resend with a different body
is the one that is rejected `422`.) The contract also says when the key is
released instead of claimed — a request that definitively failed without
starting work frees its key, while one whose outcome the server could not
characterise (a 5xx, an upstream timeout) keeps it. Retrying under one key is
only safe where that key is still spendable, so that is exactly what the default
policy retries.

The default policy:

| Condition | Behaviour |
|---|---|
| Retried | connect-phase transport failures (connection refused, connect timeout, no pooled connection, proxy error) — the request never reached the server, so the key was never claimed |
| Retried, at the server's pace | a `429` carrying `Retry-After` (queue full, out of credits, a concurrency limit) — a reject that started no work, so the key is released. The delay is the one the server named, not a guess |
| Retried, at the server's pace | the answers that pace a resend of the *same* key for work already running: a `deadline_exceeded` `504` carrying `Retry-After` (Comfy stopped holding the connection at its own bound; the contract says to retry with the same key, which collects that generation rather than dispatching another), and a `generation_in_progress` `409` carrying `Retry-After` (the same key, asked for again before the generation finished). One `run()` rides that loop to the finished result |
| Not retried | every other 4xx — `400`/`content_policy_violation`, `404`, any `409` that is not the paced `generation_in_progress` one above (`hash_mismatch` carries a `Retry-After` and is still deterministic), `422`, `401`, `402` — because asking again cannot change a deterministic refusal. A `429` with no `Retry-After` is not asking to be asked again either |
| Not retried by default | anything whose outcome is unknown: any **other 5xx response** — including the router's `service_unavailable` `503` (which asks a caller to retry with backoff but says nothing about the key), a `504` carrying no `Retry-After` (the router sends it only when it holds a generation to collect), and a `504` that is `provider_timeout` rather than `deadline_exceeded` — and a client-side timeout where the server may still be generating. The key stays claimed for these, so a same-key retry comes back `422 idempotency_key_reuse` and hides the real error — while a fresh-key retry is the second billed generation the one-key rule exists to prevent |
| Budget | 60 seconds of **total elapsed time** from the first attempt, not a number of attempts. The collect loop gets its own, longer budget: 1200 seconds, two server deadline windows, so it can outlast the deadline that started it |
| Backoff | 0.5s doubling to a 15s ceiling, with full jitter (each wait is drawn from `[0, ceiling]`), clamped to whatever is left of the budget. A `Retry-After` the server named is used as given instead |

The budget bounds when the *last* attempt may **start**; an attempt already
running is never interrupted by it, so a slow generation is never abandoned
half-way. The worst-case wall clock for a call is therefore the budget plus one
`timeout`.

Tune or disable it per client:

```python
from comfy_sdk import Comfy, NO_RETRY, RetryPolicy

Comfy(retry=NO_RETRY)                              # exactly one attempt, ever
Comfy(retry=RetryPolicy(max_elapsed=300.0))        # fast classes: five minutes
Comfy(retry=RetryPolicy(collect_max_elapsed=60.0)) # bound the collect loop
Comfy(retry=RetryPolicy(retry_collectable=False))  # raise the 504/409 instead

client.models.retry                                # the policy in force, read-only
```

There are two budgets because the classes have two shapes. `max_elapsed` governs
the fast ones — a connect failure, a paced `429` — which resolve in seconds or
not at all, so an unreachable host gives up in a minute rather than pinning a
caller (a whole thread, on the sync client) for longer. `collect_max_elapsed`
governs the collect loop alone, and is twenty minutes because that is the one
class that has to outlast a *server-side* bound: a `deadline_exceeded` `504`
arrives at Comfy's own ten-minute deadline, so a single-window budget would
already be spent when it lands and the collect attempt it exists for would never
start. Nothing else pays for that room.

A note on what the default trades: the collect rule is
`spec/router-openapi.yaml`'s, so it binds Comfy Router — but
`COMFY_ROUTER_BASE_URL` can name a deployment that applies the v2 rule instead
and keeps the key claimed across the `504`. There the collect resend comes back
`422 idempotency_key_reuse` in place of the real `504`. Set
`retry_collectable=False` on such a deployment.

Other 5xx responses and client-side timeouts are the cases left out by default,
and for the same reason. `run` holds the connection open while the server
generates, so neither one tells you whether the generation happened, and no
contract says the key survives them — retrying either starts a second generation
unless the server replays the repeated key rather than re-running it, and
against a server that *rejects* it instead the retry simply cannot succeed.
Against a deployment that does replay, opt in:

```python
Comfy(retry=RetryPolicy(max_elapsed=1200.0, retry_possibly_in_flight=True))
```

Raise `max_elapsed` when you do: one full-length client timeout on a run spends
many times the default 60-second budget on its own, leaving no room for the
retry you just asked for. `collect_max_elapsed` does not help here — that budget
is the collect class's alone.

`retry` governs `client.models` only. `submit()`/`run()` on the client keep
their own 429 handling, which follows the server's `Retry-After`.

### Collecting a generation after a lost response

Everything above is about the retry the SDK makes *for* you. When it gives up —
or when you turned it off — the failure that reaches you may still be a
generation that ran and was billed: a `504` where the server stopped holding the
connection at its own deadline, or a connection that dropped while it was
generating. Recovering that generation means asking again under the **same**
`Idempotency-Key`, and if `run` minted the key for you then that key is the one
thing you need and the one thing you never saw.

So every exception `models.run` raises carries it:

```python
import httpx
from comfy_sdk import Comfy, ComfyError

with Comfy() as client:
    try:
        result = client.models.run("acme/flux/dev", {"prompt": "a cat"})
    # Both, and the second is not optional: a dropped connection or a read
    # timeout — one of the two cases this section is about — never reached a
    # response to translate, so it arrives as the `httpx` error it was, not as
    # a `ComfyError`. Catching only `ComfyError` misses exactly the failure the
    # replay exists for.
    except (ComfyError, httpx.HTTPError) as exc:
        key = exc.idempotency_key
        if key is None:
            raise  # Nothing to replay under; a fresh key would re-run and re-bill.
        # Later — after `exc.retry_after` seconds, if the server named a pace.
        result = client.models.run(
            "acme/flux/dev", {"prompt": "a cat"}, idempotency_key=key
        )
```

Against a deployment that replays a claimed key, the second call returns the
original generation's result rather than starting a new one; while that
generation is still running it is refused instead, with a `Retry-After` saying
when to ask. Send the same arguments you sent the first time — a repeated key
with a *different* body is rejected outright.

Three attributes carry this:

| Attribute | Value |
|---|---|
| `exc.idempotency_key` | the key that call was made under — the one `run` minted, or the one you passed. Present on every exception `models.run` raises, including a transport failure with no response at all (`httpx.ConnectError`, a read timeout), a cancelled `await`, and a `RouterError` whose `error_type` this SDK version does not recognise |
| `exc.request_id` | the server's `X-Comfy-Request-Id` for the call, when the response carried one — the id to quote in a support request. `None` when there was no response, or none of that header |
| `exc.retry_after` | seconds the server asked you to wait before asking again, from `Retry-After`. `None` when it named no pace |

All three read as `None` rather than raising on any exception `models.run`
raises, so a handler never has to guard the attribute access itself.

`idempotency_key` is `None` on errors from *other* surfaces, though — it is
`models.run` that records it, and `submit()` sends a key without stamping one.
So `None` means "this SDK did not record a key for you", **not** "no key was
sent, resend freely": check for it before replaying, as the snippet above does,
rather than passing it straight back into `idempotency_key=` where `None` means
"mint a fresh one" and starts a second billed generation.

## Sync and async

`Comfy` and `AsyncComfy` expose the identical surface — swap the import and
add `await` / `async for`:

```python
from comfy_sdk import AsyncComfy

async def main() -> None:
    async with AsyncComfy(api_key="comfyui-...") as client:
        wf = client.workflows.from_file("workflow_api.json")
        job = await client.run(wf)
        await job.outputs[0].to_file("out.png")
        with open("out.bin", "wb") as stream:
            await job.outputs[0].to_stream(stream)
```

## Typed errors

`comfy_sdk` translates the API's error envelope into a small set of
exceptions, all importable from the top-level package and all subclasses of
`ComfyError`:

Catch these SDK-level exceptions around `Comfy`/`AsyncComfy` methods. Public
asset, job, event, and output helpers translate protocol errors, so catches of
`comfy_low.errors.*` belong only around direct low-level transport calls.

- `Unauthorized`, `Forbidden`, `NotFound` — auth and lookup failures.
- `InvalidWorkflow`, `WorkflowFormatUi` — the graph itself was rejected;
  `WorkflowFormatUi` specifically means a UI-export (`nodes`/`links`/
  `last_node_id`) was submitted instead of the API-format graph — the SDK
  catches this locally before it ever reaches the server.
- `MissingAsset` — a `core/ASSET` reference could not be resolved.
- `HashMismatch`, `BlobNotFound` — asset upload/dedup failures.
- `IdempotencyKeyReuse` — the `Idempotency-Key` was reused. `submit()` (and
  `run()`) attach a fresh key to every call, so an accidental exact resend never
  runs the workflow twice. Keys are single-use — reject-on-duplicate, there is
  no replay — so if you pass your own `idempotency_key=` and reuse it, the second
  call raises this. After an ambiguous failure (e.g. a timeout where you don't
  know if the job was created), poll or list your jobs rather than resubmitting
  with the same key.
- `InsufficientCredits` — the account can't afford the job.
- `QueueFull` — backpressure; carries `.retry_after` seconds. `client.submit`
  retries 429 responses with `Retry-After` for a bounded budget (including
  deployment warm-up), then raises the translated error if backpressure remains.
- `JobFailed` — a job reached a non-`succeeded` terminal state; `.error`
  carries node-level detail when the platform provided one.

```python
from comfy_sdk import JobFailed, QueueFull, Unauthorized

try:
    result = client.run(wf)
except JobFailed as e:
    print(e.error)
except Unauthorized:
    print("check your api_key")
```

## Architecture — two layers

* **`comfy_low`** — generated protocol bindings. Pydantic v2 models generated
  from `spec/openapi.yaml` (`src/comfy_low/models/_generated.py`, committed;
  regenerate with `scripts/gen_models.sh`, CI fails on drift) plus a thin
  hand-written `httpx` transport (sync + async), one function per `operationId`,
  with the mandatory escape hatches: raw response access, unbuffered/streaming
  bodies, all headers, and per-request timeout/abort. Boring and replaceable.

* **`comfy_sdk`** — the idiomatic layer integrators import. This is where the
  value lives: blake3 content-addressed dedup-upload, `core/ASSET`
  substitution, idempotent submit, live SSE with reconnect, poll-authoritative
  `run()`, range-aware downloads, and typed exceptions mapping the error
  envelope.

`spec/openapi.yaml` is a one-way vendored copy of the canonical Comfy API v2
contract — do not hand-edit it (see `spec/README.md`). It's synced
periodically from that canonical contract, stripped of anything tagged
`internal`, and pinned by `spec/VERSION`.

## Related projects

Clients for the same Comfy API v2 contract:

| Project | Language | Package |
|---|---|---|
| [comfy-python-sdk](https://github.com/Comfy-Org/comfy-python-sdk) | Python | `comfy-sdk` |
| [comfy-typescript-sdk](https://github.com/Comfy-Org/comfy-typescript-sdk) | TypeScript | `@comfyorg/sdk` |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the uv-based setup, the full list of
checks CI requires, and why `src/comfy_low/models/_generated.py` must never be
hand-edited.

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest -v
```

Regenerating and checking the vendored protocol layer (a separate CI job):

```bash
pip install -e ".[codegen]"
bash scripts/gen_models.sh       # regenerate comfy_low models from spec/openapi.yaml
python scripts/check_drift.py    # same check CI runs; fails if committed models drifted
```

## Releases

Releases are published to PyPI from a GitHub Release (tag `vX.Y.Z`) by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml), using
PyPI's Trusted Publishing (OIDC) — no API token is stored in this repo.
