"""A stdlib-only stub of the Comfy API v2 server, run in a background thread.

Keeps the SDK's own test suite independent of a real v2 server or proxy. Each
test configures ``server.state`` to drive a specific scenario (dedup hit, hash
mismatch, queue-full-then-ok, SSE reconnect, ...); the ``server`` fixture points
the SDK at the stub by setting ``COMFY_BASE_URL`` *and*
``COMFY_ROUTER_BASE_URL``.

Both, because the SDK speaks to two surfaces: the ``/api/v2`` deployment (jobs,
assets) and Comfy Router (``/v2/models/{provider}/{model}``), which is a
different host in production. This one stub answers both route families, so a
test that exercises either gets a single server — while a test that is *about*
the two being separate points ``COMFY_ROUTER_BASE_URL`` at ``second_server``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote

import pytest

from comfy_sdk import API_KEY_ENV_VAR, BASE_URL_ENV_VAR, ROUTER_BASE_URL_ENV_VAR


@dataclass
class ServerState:
    # Blobs the platform "already has" (for the dedup fast-path).
    known_hashes: set[str] = field(default_factory=set)
    # The authoritative server-side hash returned for uploads.
    server_hash: str = "blake3:" + "ab" * 32
    # If set, POST /assets rejects with 409 hash_mismatch.
    reject_hash_mismatch: bool = False
    # Bytes served by GET /assets/{id}/content.
    content_bytes: bytes = b"\x89PNG-stub-output-bytes-0123456789"
    # Require an Authorization header (Cloud/serverless).
    require_auth: bool = False
    # POST /jobs returns 429 queue_full this many times before succeeding.
    queue_full_times: int = 0
    # Like `queue_full_times`, but the 429 carries no Retry-After header at
    # all — the bare-`queue_full` path, which retries using the client's
    # default pause rather than a server-given delay.
    queue_full_times_no_retry_after: int = 0
    # POST /jobs answers 429 queue_full with this literal Retry-After header
    # value once, then succeeds — for a test to send an out-of-range value
    # (e.g. a huge number, to prove the client clamps to its retry budget,
    # or a negative one, to prove a malformed header doesn't crash the
    # sync loop or busy-loop the async one).
    queue_full_retry_after_header: str | None = None
    # POST /jobs returns a 429 naming a code OTHER than `queue_full` (with a
    # Retry-After header) this many times before succeeding — the contract
    # disambiguates a retryable 429 by status + Retry-After, not by `code`
    # (e.g. `deployment_not_ready` on a serverless cold start).
    retryable_429_times: int = 0
    retryable_429_code: str = "deployment_not_ready"
    # POST /jobs returns this error envelope (status, code) instead of 201.
    job_error: tuple[int, str] | None = None
    # GET /jobs/{id} answers 404 job_not_found instead of the job.
    job_not_found: bool = False
    # GET /jobs/{id}/events answers this (status, code) instead of connecting.
    events_error: tuple[int, str] | None = None
    # Number of GET /jobs/{id} polls before the job reports succeeded.
    polls_to_succeed: int = 1
    # Terminal status the job reaches.
    terminal_status: str = "succeeded"
    # SSE behavior: "reconnect" drops the first stream before terminal;
    # "stall" sends a couple frames then holds the connection open, silent
    # (a "zombie": no terminal, no close) for `stall_seconds`.
    sse_mode: str = "normal"
    stall_seconds: float = 2.0
    # GET /jobs/{id}/events answers 501 not_implemented — a surface without SSE.
    events_not_implemented: bool = False
    # If set, GET /assets/{id}/content responds 302 to this URL instead of
    # serving bytes directly (simulates a signed-URL redirect to another host).
    redirect_content_to: str | None = None
    # Outputs a succeeded job reports; None means the single default `_OUTPUT`.
    # Set to a list of output dicts (see `_output_json`) to test a job with
    # multiple outputs, each backed by a distinct asset id.
    job_outputs: list[dict] | None = None
    # GET /jobs/{id}/workflow response. `job_workflow_not_found=True` answers
    # 404 job_not_found instead, for the missing-job path.
    job_workflow_graph: dict[str, Any] = field(
        default_factory=lambda: {"3": {"class_type": "KSampler", "inputs": {}}}
    )
    job_workflow_format: str = "api"
    job_workflow_not_found: bool = False
    # GET /jobs/{id}/logs response body; None answers 204, the contract's normal
    # "this job has no log". `job_logs_not_found=True` answers 404 instead, for
    # the missing-job path. `omit_logs_link=True` drops `urls.logs` from every
    # job, standing in for a surface that serves no logs at all.
    job_logs: dict[str, Any] | None = None
    job_logs_not_found: bool = False
    omit_logs_link: bool = False
    # Serializes `urls.logs` as "" instead of omitting it, the shape a server
    # that forgot an omit-empty tag would emit.
    empty_logs_link: bool = False
    # Serves `urls.logs` at a path a synthesized `/jobs/{id}/logs` would never
    # produce, so a test can prove the SDK followed the link rather than built
    # the path — at the suite's root mount the two otherwise coincide.
    logs_link_path: str | None = None
    job_logs_request_count: int = 0
    # Path of the most recent logs request, so a test can assert WHICH url was used.
    last_job_logs_path: str | None = None

    # --- POST /v2/models/{provider}/{model} (the awaited model run) ---
    # The provider's native payload the run resolves to. Deliberately not a
    # Comfy-shaped envelope: the SDK must hand it back untouched.
    model_run_result: dict[str, Any] = field(
        default_factory=lambda: {
            "images": [{"url": "http://example.invalid/gen.png", "width": 1024, "height": 1024}],
            "seed": 42,
            "timings": {"inference": 3.5},
        }
    )
    # Seconds the run holds the connection before answering — stands in for a
    # generation the server polls internally, so a client whose timeout is too
    # short aborts a healthy run.
    model_run_delay: float = 0.0
    # (status, code) answered instead of the result.
    model_run_error: tuple[int, str] | None = None
    # A model run fails this many times before serving the result — the
    # transient-failure-then-success path a retry policy exists for. Decremented
    # per request, and checked *before* `model_run_error`, which is the
    # permanent-failure knob.
    model_run_fail_times: int = 0
    # (status, code) each of those transient failures answers with.
    model_run_transient_error: tuple[int, str] = (503, "internal_error")
    # Status code for a successful run (201 exercises the created-shaped path).
    model_run_status: int = 200
    # Answer a successful run with a body that is not JSON at all — a proxy
    # interstitial served under a 200, a response truncated mid-stream. The
    # generation ran and was billed; only the result is unreadable.
    model_run_undecodable_body: bool = False
    # Model the deployment `retry_possibly_in_flight` exists for: one that
    # *replays* a repeated Idempotency-Key rather than rejecting it, so a key
    # is released rather than claimed when a request fails 5xx. Default False
    # is the vendored contract (reject-on-duplicate, no replay), under which a
    # same-key retry after a 5xx can only come back 422.
    model_run_replays_idempotency_key: bool = False
    # The stronger property the flag above does *not* model: the generation ran
    # to completion server side and only the *response* was lost (the
    # `deadline_exceeded` 504 the replay contract is written for). With this on,
    # a failed run whose outcome is unknown records its result against the key,
    # and a later request presenting that key is answered with the recorded
    # result — without running the model again. Kept separate because the flag
    # above only releases the key: on its own it lets a same-key resend *re-run*
    # the model, which is the double charge, not the replay.
    model_run_replays_lost_result: bool = False
    # The narrower carry the router contract describes for `deadline_exceeded`:
    # the reservation survives the 504 with a handle to the generation stamped
    # on it, so the SAME key presented again *collects* that generation instead
    # of being rejected 422. Only the 504 behaves that way — every other 5xx
    # still claims the key — which is what makes this distinct from
    # `model_run_replays_idempotency_key`, the whole-deployment replay the
    # `retry_possibly_in_flight` opt-in exists for.
    model_run_collects_after_deadline: bool = False
    # Seconds sent as Retry-After alongside `model_run_error` /
    # `model_run_transient_error`, for the failures the policy is allowed to
    # pace itself against (a 429, a `deadline_exceeded` 504, an in-progress
    # 409). `None` sends no header at all, which is the same failure the policy
    # must *not* retry.
    model_run_retry_after: str | None = None
    # Sent as X-Comfy-Request-Id alongside a failed run. `None` sends no header,
    # which is the response an intermediary that never reached the router gives.
    model_run_request_id: str | None = None
    # Answer a repeated model-run key with the v2 jobs rule (422
    # idempotency_key_reuse) instead of the router contract's replay-or-409.
    # Default False: the run route's vendored contract answers a consumed,
    # non-replayable key 409 invalid_input in Router's own shape. True models
    # the deployment `COMFY_ROUTER_BASE_URL` can name that applies the v2 rule.
    model_run_v2_key_rule: bool = False
    # Answer model-run failures in Router's own error shape -- the coarse bucket
    # on `X-Comfy-Error-Type` plus a `{detail, error_type}` body -- instead of
    # the v2 `{error: {code, message}}` envelope. The model-run route is
    # fronted by Router, so this is the shape a real deployment's 504 arrives
    # in, and the bucket-keyed collect rule has to read it.
    model_run_router_error_shape: bool = False

    # --- counters the tests assert on ---
    upload_count: int = 0
    from_hash_count: int = 0
    head_count: int = 0
    delete_count: int = 0
    deleted_assets: set[str] = field(default_factory=set)
    job_poll_count: int = 0
    events_connect_count: int = 0
    submit_count: int = 0
    last_workflow: dict[str, Any] | None = None
    # The full POST /jobs body (so a test can assert `extra_data` is present
    # with the right value, or absent entirely, without touching last_workflow).
    last_jobs_body: dict[str, Any] | None = None
    # Idempotency-Key -> job id of the first (accepted) request, so a reuse of
    # the same key can be detected and rejected (single-use, no replay).
    idempotency: dict[str, str] = field(default_factory=dict)
    # Raw bytes of the last POST /assets multipart body (so tests can inspect
    # the parts actually sent — e.g. how many `tags` fields were included).
    last_upload_body: bytes = b""
    # Authorization header seen on the most recent request (any method).
    # `None` before any request; `""` if a request arrived without one.
    last_auth_header: str | None = None
    last_user_agent: str | None = None
    model_run_count: int = 0
    # The raw JSON body of the last model run — the partner model's *native*
    # input, with no `{model, arguments}` envelope around it, exactly as Router
    # forwards it upstream.
    last_model_run_body: dict[str, Any] | None = None
    # The two path segments of the last model run, percent-DECODED, so a test
    # asserts the id the caller passed rather than a particular encoding of it.
    last_model_run_provider: str | None = None
    last_model_run_model: str | None = None
    # ...and the raw, still-encoded request path, for the tests that are about
    # the encoding itself.
    last_model_run_path: str | None = None
    # Every Idempotency-Key seen on a model run, in arrival order (`None`
    # records a run that arrived without the header at all).
    model_run_idempotency_keys: list[str | None] = field(default_factory=list)
    # Keys a model run has *claimed*, so a reuse can be rejected exactly as
    # POST /jobs rejects one. Kept apart from `idempotency` only so a model
    # test cannot perturb a workflow test's bookkeeping.
    model_run_idempotency: dict[str, str] = field(default_factory=dict)
    # Idempotency-Key -> the result recorded for it under
    # `model_run_replays_lost_result`, served verbatim to a later request
    # presenting the same key.
    model_run_replay_store: dict[str, dict[str, Any]] = field(default_factory=dict)
    # How many times the model actually *ran*, as distinct from how many
    # requests arrived (`model_run_count`). A replay serves a recorded result
    # and does not increment this, which is what lets a test tell a real replay
    # apart from a second generation that merely returns an equal payload.
    model_run_generations: int = 0


def _asset_json(asset_id: str, hash_: str, created_new: bool, size: int) -> dict:
    return {
        "id": asset_id,
        "hash": hash_,
        "size_bytes": size,
        "content_type": "image/png",
        "file_path": "photo.png",
        "created_new": created_new,
        "created_at": "2026-07-10T18:00:00Z",
        "url": "http://example.invalid/blob",
        "url_expires_at": "2026-07-10T19:00:00Z",
        # Retention deadline for the asset itself — distinct from url_expires_at
        # above. Omitted (-> None) for a plain upload with no producing job.
        "expires_at": "2026-08-09T18:00:00Z",
    }


def _job_json(
    job_id: str,
    status: str,
    outputs: list[dict] | None = None,
    *,
    omit_logs_link: bool = False,
    empty_logs_link: bool = False,
    logs_link_path: str | None = None,
) -> dict:
    return {
        "id": job_id,
        "status": status,
        "created_at": "2026-07-10T18:20:00Z",
        "started_at": None,
        "completed_at": None,
        "expires_at": "2026-07-11T18:20:00Z",
        "queue_position": 0,
        "progress": None,
        "outputs": outputs or [],
        "error": None,
        "metrics": {"queue_ms": 9000, "execution_ms": None},
        "urls": {
            "self": f"/api/v2/jobs/{job_id}",
            "events": f"/api/v2/jobs/{job_id}/events",
            "cancel": f"/api/v2/jobs/{job_id}/cancel",
            **(
                {}
                if omit_logs_link
                else {
                    "logs": ""
                    if empty_logs_link
                    else (logs_link_path or f"/api/v2/jobs/{job_id}/logs")
                }
            ),
        },
    }


def _output_json(node_id: str, asset_id: str) -> dict:
    return {
        "node_id": node_id,
        "name": f"{asset_id}.png",
        "type": "image",
        "content_type": "image/png",
        "size_bytes": 33,
        "id": asset_id,
        "hash": None,
        "url": "http://example.invalid/out",
        "url_expires_at": "2026-07-10T19:20:00Z",
    }


_OUTPUT = _output_json("13", "asset_out_01")


def _make_handler(state: ServerState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def handle_one_request(self) -> None:
            # A test that deliberately aborts a slow request (an over-short
            # client timeout against `model_run_delay`) closes the socket while
            # this thread is still writing. That is the scenario under test,
            # not a server fault — so don't dump a traceback for it. Only these
            # two exception types are swallowed; anything else still surfaces.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        # -- helpers --
        def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _raw(self, status: int, body: bytes, content_type: str) -> None:
            """A response whose body is *not* JSON — the case a client that
            calls ``.json()`` unguarded on a success status falls over on."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, status: int, code: str, message: str = "err") -> None:
            self._json(status, {"error": {"code": code, "message": message}})

        def _auth_ok(self) -> bool:
            # Record what actually arrived (regardless of whether auth is
            # required) so a test can assert the bearer token was — or was
            # not — attached to a given request.
            state.last_auth_header = self.headers.get("Authorization", "")
            state.last_user_agent = self.headers.get("User-Agent", "")
            if not state.require_auth:
                return True
            return bool(state.last_auth_header)

        def _read_body(self) -> bytes:
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        # -- HEAD --
        def do_HEAD(self) -> None:
            m = re.match(r"/api/v2/assets/by-hash/(.+)$", self.path)
            if m:
                state.head_count += 1
                hash_ = m.group(1)
                self.send_response(200 if hash_ in state.known_hashes else 404)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        # -- DELETE --
        def do_DELETE(self) -> None:
            if not self._auth_ok():
                self._err(401, "unauthorized", "no key")
                return
            m = re.match(r"/api/v2/assets/([^/]+)$", self.path)
            if m:
                state.delete_count += 1
                state.deleted_assets.add(m.group(1))
                self.send_response(204)
                self.end_headers()
                return
            self._err(404, "not_found")

        # -- GET --
        def do_GET(self) -> None:
            if not self._auth_ok():
                self._err(401, "unauthorized", "no key")
                return

            m = re.match(r"/api/v2/assets/([^/]+)/content$", self.path)
            if m:
                if m.group(1) in state.deleted_assets:
                    self._err(404, "not_found")
                    return
                if state.redirect_content_to:
                    self._redirect(state.redirect_content_to)
                else:
                    self._serve_content()
                return
            m = re.match(r"/api/v2/assets/([^/]+)$", self.path)
            if m:
                if m.group(1) in state.deleted_assets:
                    self._err(404, "not_found")
                    return
                self._json(200, _asset_json(m.group(1), state.server_hash, False, 33))
                return
            m = re.match(r"/api/v2/jobs/([^/]+)/events$", self.path)
            if m:
                self._serve_events(m.group(1))
                return
            m = re.match(r"/api/v2/jobs/([^/]+)/logs$", self.path)
            if m:
                state.last_job_logs_path = self.path
                self._serve_job_logs()
                return
            m = re.match(r"/api/v2/jobs/([^/]+)/workflow$", self.path)
            if m:
                self._serve_job_workflow(m.group(1))
                return
            m = re.match(r"/api/v2/jobs/([^/]+)$", self.path)
            if m:
                self._serve_job(m.group(1))
                return
            self._err(404, "not_found")

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_content(self) -> None:
            data = state.content_bytes
            rng = self.headers.get("Range")
            if rng:
                mm = re.match(r"bytes=(\d+)-(\d+)", rng)
                if mm:
                    start, end = int(mm.group(1)), int(mm.group(2))
                    chunk = data[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_job(self, job_id: str) -> None:
            if state.job_not_found:
                self._err(404, "job_not_found", "no such job")
                return
            state.job_poll_count += 1
            if state.job_poll_count >= state.polls_to_succeed:
                status = state.terminal_status
                if status == "succeeded":
                    raw = state.job_outputs if state.job_outputs is not None else [_OUTPUT]
                    # Stamp the producing job id, same as a real server would.
                    outputs = [{**o, "job_id": job_id} for o in raw]
                else:
                    outputs = []
            else:
                status = "running"
                outputs = []
            self._json(
                200,
                _job_json(
                    job_id,
                    status,
                    outputs,
                    omit_logs_link=state.omit_logs_link,
                    empty_logs_link=state.empty_logs_link,
                    logs_link_path=state.logs_link_path,
                ),
            )

        def _serve_job_logs(self) -> None:
            state.job_logs_request_count += 1
            if state.job_logs_not_found:
                self._err(404, "job_not_found", "no such job")
                return
            if state.job_logs is None:
                self.send_response(204)
                self.end_headers()
                return
            self._json(200, state.job_logs)

        def _serve_job_workflow(self, job_id: str) -> None:
            if state.job_workflow_not_found:
                self._err(404, "job_not_found", "no such job")
                return
            self._json(
                200,
                {"workflow": state.job_workflow_graph, "format": state.job_workflow_format},
            )

        def _serve_events(self, job_id: str) -> None:
            state.events_connect_count += 1
            if state.events_error is not None:
                status, code = state.events_error
                self._err(status, code, f"events error {code}")
                return
            if state.events_not_implemented:
                self._err(501, "not_implemented", "SSE is not supported on this surface")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def frame(event: str, data: dict) -> None:
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()

            if state.sse_mode == "reconnect" and state.events_connect_count == 1:
                # First connection: a progress frame, then drop without terminal.
                frame("progress", {"value": 0.4, "nodes_done": 4, "nodes_total": 10})
                return
            if state.sse_mode == "stall" and state.events_connect_count == 1:
                # First connection: a couple frames, then hold the socket open and
                # silent (no terminal, no close) — a "zombie" the client must
                # recover from via its read-idle timeout + poll fallback.
                frame("status", {"status": "running"})
                frame("progress", {"value": 0.3, "nodes_done": 3, "nodes_total": 10})
                time.sleep(state.stall_seconds)
                return
            # Normal (or the reconnect's 2nd connection): full run to terminal.
            frame("status", {"status": "running"})
            frame("progress", {"value": 0.5, "nodes_done": 5, "nodes_total": 10})
            frame("output", _OUTPUT)
            frame("status", {"status": state.terminal_status})

        # -- POST --
        def do_POST(self) -> None:
            if not self._auth_ok():
                self._read_body()
                self._err(401, "unauthorized", "no key")
                return

            if self.path == "/api/v2/assets":
                self._post_assets()
                return
            if self.path == "/api/v2/assets/from-hash":
                self._post_from_hash()
                return
            if self.path == "/api/v2/jobs":
                self._post_jobs()
                return
            # Comfy Router's invocation route — a different surface from the
            # `/api/v2` paths above (a different host in production; the same
            # stub here, with `COMFY_ROUTER_BASE_URL` pointed at it). The two
            # segments are the model id, so they are matched rather than
            # compared to a fixed string.
            m = re.match(r"/v2/models/([^/]+)/([^/]+)$", self.path)
            if m:
                self._post_model_run(m.group(1), m.group(2))
                return
            m = re.match(r"/api/v2/jobs/([^/]+)/cancel$", self.path)
            if m:
                self._json(
                    200,
                    _job_json(
                        m.group(1),
                        "canceling",
                        omit_logs_link=state.omit_logs_link,
                        empty_logs_link=state.empty_logs_link,
                        logs_link_path=state.logs_link_path,
                    ),
                )
                return
            self._read_body()
            self._err(404, "not_found")

        def _post_assets(self) -> None:
            state.upload_count += 1
            body = self._read_body()
            state.last_upload_body = body
            if state.reject_hash_mismatch:
                self._err(409, "hash_mismatch", "bytes do not match expected_hash")
                return
            self._json(201, _asset_json("asset_uploaded_01", state.server_hash, True, len(body)))

        def _post_from_hash(self) -> None:
            state.from_hash_count += 1
            body = json.loads(self._read_body() or b"{}")
            if body.get("hash") in state.known_hashes:
                self._json(201, _asset_json("asset_dedup_01", body["hash"], False, 33))
            else:
                self._err(404, "blob_not_found", "no such blob")

        def _post_model_run(self, provider: str, model: str) -> None:
            state.model_run_count += 1
            # Decoded, because the SDK percent-encodes each segment and a real
            # origin server decodes it before routing — asserting the encoded
            # form everywhere would pin tests to an encoding rather than to the
            # id the caller passed. `last_model_run_path` keeps the raw form
            # for the tests that are about the encoding.
            state.last_model_run_provider = unquote(provider)
            state.last_model_run_model = unquote(model)
            state.last_model_run_path = self.path
            state.last_model_run_body = json.loads(self._read_body() or b"{}")
            key = self.headers.get("Idempotency-Key")
            state.model_run_idempotency_keys.append(key)

            # Checked before the reject-on-duplicate rule below: a deployment
            # that replays a claimed key answers with the recorded result
            # rather than rejecting the resend, and the model does not run
            # again — which is the whole point of asking under the same key.
            if key and key in state.model_run_replay_store:
                self._json(
                    200,
                    state.model_run_replay_store[key],
                    headers={"Idempotent-Replayed": "true"},
                )
                return

            # The run route's own reuse answer (spec/router-openapi.yaml):
            # a consumed key whose record cannot be replayed is refused 409
            # invalid_input in Router's shape — never 422. The v2 jobs rule
            # stays reachable behind `model_run_v2_key_rule` for the test that
            # models a non-Router deployment.
            if key and key in state.model_run_idempotency:
                if state.model_run_v2_key_rule:
                    self._err(422, "idempotency_key_reuse", "Idempotency-Key already used")
                    return
                self._json(
                    409,
                    {
                        "detail": "Idempotency-Key already consumed; use a new key",
                        "error_type": "invalid_input",
                    },
                    headers={"X-Comfy-Error-Type": "invalid_input"},
                )
                return

            if state.model_run_delay:
                # The server holding the connection while it polls upstream.
                time.sleep(state.model_run_delay)

            def claim_if_outcome_unknown(status: int, code: str) -> None:
                # The contract releases a key for a request that definitively
                # failed without starting work (a 4xx) and keeps it claimed
                # when the outcome is unknown (a 5xx). Two exceptions: a
                # replaying deployment (what the opt-in is for), and the
                # router's `deadline_exceeded` 504, whose reservation survives
                # so the same key can collect the generation still running.
                #
                # The bucket is part of that second exception and not an
                # afterthought: the carve-out the router documents is for
                # `deadline_exceeded` specifically, not for the status, which it
                # shares with `provider_timeout`. Keying on the status alone
                # would make the stub more permissive than the contract it
                # stands in for, and an SDK regression that resent a
                # `provider_timeout` 504 would pass here while meeting a 422 on
                # a real server.
                if not key or status < 500:
                    return
                if state.model_run_replays_idempotency_key:
                    return
                if (
                    state.model_run_collects_after_deadline
                    and status == 504
                    and code == "deadline_exceeded"
                ):
                    return
                state.model_run_idempotency[key] = "claimed"

            def fail(status: int, code: str, message: str) -> None:
                claim_if_outcome_unknown(status, code)
                if state.model_run_replays_lost_result and key and status >= 500:
                    # The generation completed; only the answer was lost. Bill
                    # it once and record it, so the same key collects it.
                    state.model_run_generations += 1
                    state.model_run_replay_store[key] = state.model_run_result
                headers: dict[str, str] = {}
                if state.model_run_retry_after is not None:
                    headers["Retry-After"] = state.model_run_retry_after
                if state.model_run_request_id is not None:
                    headers["X-Comfy-Request-Id"] = state.model_run_request_id
                if state.model_run_router_error_shape:
                    # What a real Router failure looks like: the coarse bucket
                    # on `X-Comfy-Error-Type` and a `{detail, error_type}` body,
                    # with no v2 `error.code` anywhere. The SDK's bucket-keyed
                    # retry rules have to survive this shape too, and nothing
                    # exercised it while the stub only ever spoke the envelope.
                    headers["X-Comfy-Error-Type"] = code
                    self._json(
                        status, {"detail": message, "error_type": code}, headers=headers or None
                    )
                    return
                self._json(
                    status,
                    {"error": {"code": code, "message": message}},
                    headers=headers or None,
                )

            if state.model_run_fail_times > 0:
                state.model_run_fail_times -= 1
                status, code = state.model_run_transient_error
                fail(status, code, f"transient model run error {code}")
                return
            if state.model_run_error is not None:
                status, code = state.model_run_error
                fail(status, code, f"model run error {code}")
                return
            if key:
                state.model_run_idempotency[key] = "done"
            state.model_run_generations += 1
            if state.model_run_undecodable_body:
                self._raw(
                    state.model_run_status,
                    b"<html><body>502 from an intermediary</body></html>",
                    "text/html",
                )
                return
            self._json(state.model_run_status, state.model_run_result)

        def _post_jobs(self) -> None:
            state.submit_count += 1
            body = json.loads(self._read_body() or b"{}")
            state.last_workflow = body.get("workflow")
            state.last_jobs_body = body
            key = self.headers.get("Idempotency-Key")

            if key and key in state.idempotency:
                # Reject-on-duplicate (single-use keys, no replay): any reuse of
                # an already-claimed key is 422 idempotency_key_reuse.
                self._err(422, "idempotency_key_reuse", "Idempotency-Key already used")
                return

            if state.queue_full_times > 0:
                state.queue_full_times -= 1
                self._json(
                    429,
                    {"error": {"code": "queue_full", "message": "full"}},
                    headers={"Retry-After": "0"},
                )
                return

            if state.queue_full_times_no_retry_after > 0:
                state.queue_full_times_no_retry_after -= 1
                self._json(429, {"error": {"code": "queue_full", "message": "full"}})
                return

            if state.queue_full_retry_after_header is not None:
                header = state.queue_full_retry_after_header
                state.queue_full_retry_after_header = None
                self._json(
                    429,
                    {"error": {"code": "queue_full", "message": "full"}},
                    headers={"Retry-After": header},
                )
                return

            if state.retryable_429_times > 0:
                state.retryable_429_times -= 1
                self._json(
                    429,
                    {"error": {"code": state.retryable_429_code, "message": "warming up"}},
                    headers={"Retry-After": "0"},
                )
                return

            if state.job_error is not None:
                status, code = state.job_error
                self._err(status, code, f"job error {code}")
                return

            job_id = f"job_{state.submit_count:02d}"
            if key:
                state.idempotency[key] = job_id
            self._json(
                201,
                _job_json(
                    job_id,
                    "queued",
                    omit_logs_link=state.omit_logs_link,
                    empty_logs_link=state.empty_logs_link,
                    logs_link_path=state.logs_link_path,
                ),
            )

    return Handler


class _Server:
    def __init__(self, httpd: ThreadingHTTPServer, state: ServerState) -> None:
        self._httpd = httpd
        self.state = state
        host, port = httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"


def _start_server() -> _Server:
    state = ServerState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    srv = _Server(httpd, state)
    srv._thread = thread  # type: ignore[attr-defined]
    return srv


def _stop_server(srv: _Server) -> None:
    srv._httpd.shutdown()
    srv._thread.join(timeout=5)  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _no_ambient_base_url(request, monkeypatch):
    """Keep a developer's own ``COMFY_BASE_URL`` out of the suite.

    ``tests/integration`` is the exception — that suite is pointed at a live
    deployment by this very variable. Restoring it through ``monkeypatch``
    leaves the environment as it was found, either way.
    """
    if "integration" in request.path.parts:
        return
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    # The Router target too: a developer with `COMFY_ROUTER_BASE_URL` exported
    # would otherwise send every `models.run` test at their own host — and the
    # default-value assertions would pass or fail on their shell, not the code.
    monkeypatch.delenv(ROUTER_BASE_URL_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_api_key(monkeypatch):
    """Never let the developer's own ``COMFY_API_KEY`` reach a test client.

    Autouse and unconditional: the SDK now falls back to that variable, so a key
    exported in the shell (or on a CI runner running the live gateway e2e job)
    would silently authenticate clients the suite builds deliberately *without*
    one — turning the no-credentials-sent assertions green for the wrong reason.
    The gateway e2e test reads the variable at import time and passes it
    explicitly, so it is unaffected.
    """
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


@pytest.fixture
def server(monkeypatch):
    srv = _start_server()
    # Clients read their target from the environment, so pointing them at the
    # stub is part of standing it up: tests just construct ``Comfy()``.
    monkeypatch.setenv(BASE_URL_ENV_VAR, srv.base_url)
    # Both targets, because the SDK has two: jobs and assets resolve under
    # `COMFY_BASE_URL`, model runs under `COMFY_ROUTER_BASE_URL`. The one stub
    # serves both route families, so pointing both here keeps a `models.run`
    # test a single-server test — the *separate*-origin cases point this second
    # variable at `second_server` themselves.
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, srv.base_url)
    try:
        yield srv
    finally:
        _stop_server(srv)


@pytest.fixture
def second_server():
    """A second, independent stub server on a different port.

    Gives a test a distinct *origin* from ``server`` — used to prove the
    bearer token is not attached to absolute follow-up links (job.urls.*)
    pointing somewhere other than the configured base_url.
    """
    srv = _start_server()
    try:
        yield srv
    finally:
        _stop_server(srv)
