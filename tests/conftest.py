"""A stdlib-only stub of the Comfy API v2 server, run in a background thread.

Keeps the SDK's own test suite independent of a real v2 server or proxy. Each
test configures ``server.state`` to drive a specific scenario (dedup hit, hash
mismatch, queue-full-then-ok, SSE reconnect, ...); the ``server`` fixture points
the SDK at the stub by setting ``COMFY_BASE_URL``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from comfy_sdk import BASE_URL_ENV_VAR


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


def _job_json(job_id: str, status: str, outputs: list[dict] | None = None) -> dict:
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
            self._json(200, _job_json(job_id, status, outputs))

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
            m = re.match(r"/api/v2/jobs/([^/]+)/cancel$", self.path)
            if m:
                self._json(200, _job_json(m.group(1), "canceling"))
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
            self._json(201, _job_json(job_id, "queued"))

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


@pytest.fixture
def server(monkeypatch):
    srv = _start_server()
    # Clients read their target from the environment, so pointing them at the
    # stub is part of standing it up: tests just construct ``Comfy()``.
    monkeypatch.setenv(BASE_URL_ENV_VAR, srv.base_url)
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
