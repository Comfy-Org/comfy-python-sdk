"""Live end-to-end test of the SDK against a serverless gateway deployment:
upload an input asset, submit a workflow that references it, poll to a
terminal state, download the output.

Skipped unless pointed at a live deployment:

    export COMFY_BASE_URL="https://<dep_id>.stg.run.comfy.app"
    export COMFY_API_KEY="comfyui-..."
    pytest tests/integration/test_gateway_e2e.py -v

The default workflow uses only core nodes (no models), so any deployment
works. Point COMFY_WORKFLOW_FILE at an API-format workflow JSON to run your
own instead: its first LoadImage node is rewired to the uploaded test input,
so it must contain one (and its distribution's models must be baked into the
target deployment).

First run streams a full multipart upload; reruns hit the by-hash dedup
fast-path (the input image is deterministic), so both upload paths get
coverage across two runs.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import zlib
from pathlib import Path

import pytest

from comfy_sdk import BASE_URL_ENV_VAR, Comfy, OutputReady, StatusChange
from comfy_sdk.events import Event

BASE_URL = os.environ.get(BASE_URL_ENV_VAR)
API_KEY = os.environ.get("COMFY_API_KEY")
INPUT_NAME = "sdk_e2e_input.png"
WORKFLOW_FILE = os.environ.get("COMFY_WORKFLOW_FILE")
JOB_TIMEOUT_S = 600  # cold start on a scale-to-zero deployment takes minutes
STREAM_CAP_S = 120  # hard wall-clock cap on stream consumption (never hang CI)
FIRST_FRAME_S = 30  # generous bound on connect-to-first-snapshot (~0.4s live)

pytestmark = pytest.mark.skipif(
    not (BASE_URL and API_KEY),
    reason="set COMFY_BASE_URL and COMFY_API_KEY to run gateway e2e tests",
)


def _gradient_png(width: int = 512, height: int = 512) -> bytes:
    """A deterministic RGB gradient PNG, stdlib-only (no PIL dependency)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + bytes(v for x in range(width) for v in (x * 255 // width, y * 255 // height, 128))
        for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


DEFAULT_WORKFLOW = {
    "1": {"class_type": "LoadImage", "inputs": {"image": ""}},
    "2": {"class_type": "ImageInvert", "inputs": {"image": ["1", 0]}},
    "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": "sdk_e2e", "images": ["2", 0]}},
}


def _edit_workflow(image_ref: object) -> tuple[dict, str]:
    """The graph to run and the node id of the LoadImage wired to ``image_ref``."""
    if WORKFLOW_FILE:
        graph = json.loads(Path(WORKFLOW_FILE).read_text())
    else:
        graph = json.loads(json.dumps(DEFAULT_WORKFLOW))
    for node_id, node in graph.items():
        if node.get("class_type") == "LoadImage":
            node["inputs"]["image"] = image_ref
            return graph, node_id
    raise AssertionError("workflow has no LoadImage node to receive the test input")


@pytest.fixture(scope="module")
def client() -> Comfy:
    # tests/conftest.py scrubs COMFY_BASE_URL so the unit suite can never reach
    # a real deployment; this module is the one that wants it back.
    os.environ[BASE_URL_ENV_VAR] = BASE_URL
    c = Comfy(api_key=API_KEY)
    yield c
    c.close()


@pytest.fixture(scope="module")
def input_asset(client: Comfy, tmp_path_factory: pytest.TempPathFactory):
    path = tmp_path_factory.mktemp("inputs") / INPUT_NAME
    path.write_bytes(_gradient_png())
    asset = client.assets.from_file(path)
    asset.commit()
    return asset


def test_upload_dedup_roundtrip(client: Comfy, input_asset) -> None:
    again = client.assets.from_bytes(_gradient_png(), filename=INPUT_NAME)
    assert again.commit() == input_asset.commit()


def test_image_edit_by_name(client: Comfy, input_asset, tmp_path) -> None:
    graph, _ = _edit_workflow(INPUT_NAME)
    job = client.run(client.workflows.from_json(graph)).wait(timeout=JOB_TIMEOUT_S)

    assert job.status == "succeeded", f"job {job.id} ended {job.status}: {job.error}"
    assert job.outputs, f"job {job.id} succeeded with no outputs"

    out_path = tmp_path / "sdk_e2e_out"
    job.outputs[0].to_file(out_path)
    data = out_path.read_bytes()
    assert data, "downloaded output is empty"
    if not WORKFLOW_FILE:
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (512, 512)


def test_stream_delivers_lifecycle(client: Comfy, input_asset) -> None:
    """SSE conformance: ``job.events()`` delivers the v1 lifecycle live.

    The gateway's v1 stream emits ``status`` (snapshot on connect, then on
    change) and ``output`` frames. Asserted contract: a StatusChange snapshot
    arrives promptly after connect, an OutputReady arrives before the terminal
    frame, iteration ends on its own at a terminal StatusChange, and the
    poll-authoritative state agrees. Duplicate frames are legal (snapshot
    semantics), so counts are never asserted — only presence and order.

    Runs after the plain submit-and-poll test, so the deployment is warm and
    only the connect-to-first-frame gap (served by the gateway itself, not the
    worker) is asserted tight.
    """
    graph, _ = _edit_workflow(INPUT_NAME)
    job = client.submit(client.workflows.from_json(graph))

    arrivals: list[tuple[float, Event]] = []
    errors: list[BaseException] = []
    t0 = time.monotonic()

    def consume() -> None:
        try:
            for ev in job.events():
                arrivals.append((time.monotonic() - t0, ev))
        except BaseException as exc:  # surfaced in the main thread below
            errors.append(exc)

    # Daemon-thread watchdog: a wedged stream fails the test instead of
    # hanging CI, and the abandoned thread cannot block interpreter exit.
    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    consumer.join(timeout=STREAM_CAP_S)
    kinds = [type(e).__name__ for _, e in arrivals]
    assert not consumer.is_alive(), (
        f"events() did not end on its own within {STREAM_CAP_S}s; frames so far: {kinds}"
    )
    if errors:
        raise errors[0]

    events = [e for _, e in arrivals]
    status_arrivals = [(gap, e) for gap, e in arrivals if isinstance(e, StatusChange)]
    assert status_arrivals, f"no StatusChange frames: {kinds}"
    first_gap = status_arrivals[0][0]
    assert first_gap < FIRST_FRAME_S, f"snapshot status took {first_gap:.1f}s to arrive"

    assert isinstance(events[-1], StatusChange), f"stream did not end on a status frame: {kinds}"
    assert events[-1].status == "succeeded", f"terminal status {events[-1].status}: {kinds}"
    assert any(isinstance(e, OutputReady) for e in events[:-1]), (
        f"no OutputReady before the terminal frame: {kinds}"
    )

    assert job.refresh().status == "succeeded"


@pytest.mark.xfail(
    reason="gateway does not yet resolve core/ASSET references in the graph; "
    "it resolves string filename references only",
    strict=False,
)
def test_image_edit_by_asset_handle(client: Comfy, input_asset, tmp_path) -> None:
    graph, load_node = _edit_workflow(INPUT_NAME)
    wf = client.workflows.from_json(graph)
    wf.set_input(load_node, "image", input_asset)
    job = client.run(wf).wait(timeout=JOB_TIMEOUT_S)

    assert job.status == "succeeded", f"job {job.id} ended {job.status}: {job.error}"
    assert job.outputs, f"job {job.id} succeeded with no outputs"
