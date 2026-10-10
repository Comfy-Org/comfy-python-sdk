"""Typed, ``match``/``case``-able streamed events.

The raw SSE frames from ``comfy_low`` (``event`` name + ``data`` dict) are lifted
into a small closed set of dataclasses so callers can pattern-match:

    for event in job.events():
        match event:
            case Progress() as p: ...
            case Preview() as pv: pv.to_pil()
            case OutputReady() as o: o.output.to_file(...)
            case StatusChange(status="succeeded"): break
            case Log() as log: ...
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from comfy_low.models import Output as LowOutput
from comfy_low.models import Progress as LowProgress
from comfy_low.sse import RawEvent

from .outputs import AsyncOutput, Output


@dataclass
class Progress:
    value: float
    message: str | None = None
    nodes_done: int | None = None
    nodes_total: int | None = None
    current_node: str | None = None
    step: int | None = None
    steps: int | None = None
    # Appended rather than slotted in beside `current_node` on purpose: the
    # fields above are a published positional order, and reordering them
    # would silently re-bind any `Progress(0.42, "KSampler 21/50", ...)`
    # built positionally by a caller.
    current_node_class: str | None = None


@dataclass
class Preview:
    node_id: str
    content_type: str
    data: bytes

    def to_pil(self) -> Any:
        """Decode the preview to a ``PIL.Image`` (requires Pillow)."""
        from io import BytesIO

        from PIL import Image  # imported lazily; Pillow is an optional extra

        return Image.open(BytesIO(self.data))


@dataclass
class OutputReady:
    output: Output | AsyncOutput


@dataclass
class StatusChange:
    status: str
    queue_position: int | None = None


@dataclass
class Log:
    level: str
    message: str


Event = Progress | Preview | OutputReady | StatusChange | Log


def _progress(data: dict[str, Any]) -> Progress:
    return Progress(
        value=float(data.get("value", 0.0)),
        message=data.get("message"),
        nodes_done=data.get("nodes_done"),
        nodes_total=data.get("nodes_total"),
        current_node=data.get("current_node"),
        current_node_class=data.get("current_node_class"),
        step=data.get("step"),
        steps=data.get("steps"),
    )


def progress_from_model(model: LowProgress) -> Progress:
    """Lift the generated progress model into the SDK's :class:`Progress`.

    The same dataclass the ``progress`` frames of a job's event stream carry,
    so a snapshot read off a job handle and one received live are the same
    type — which is what makes ``case Progress()`` match either. Field for
    field with the model; nothing is dropped.
    """
    return Progress(
        value=model.value,
        message=model.message,
        nodes_done=model.nodes_done,
        nodes_total=model.nodes_total,
        current_node=model.current_node,
        current_node_class=model.current_node_class,
        step=model.step,
        steps=model.steps,
    )


def _preview(data: dict[str, Any]) -> Preview:
    raw = data.get("data_base64", "")
    try:
        decoded = base64.b64decode(raw)
    except (ValueError, TypeError):
        decoded = b""
    return Preview(
        node_id=data.get("node_id", ""),
        content_type=data.get("content_type", "application/octet-stream"),
        data=decoded,
    )


def _status(data: dict[str, Any]) -> StatusChange:
    return StatusChange(status=data.get("status", ""), queue_position=data.get("queue_position"))


def _log(data: dict[str, Any]) -> Log:
    return Log(level=data.get("level", "info"), message=data.get("message", ""))


def event_from_raw(raw: RawEvent, output_binder: Any) -> Event | None:
    """Lift a raw SSE frame into a typed event.

    ``output_binder`` wraps the low-level output model into an SDK ``Output`` /
    ``AsyncOutput`` (the only event type that needs the transport, for download).
    Unknown event names return ``None`` so the iterator can skip them.
    """
    match raw.event:
        case "progress":
            return _progress(raw.data)
        case "preview":
            return _preview(raw.data)
        case "status":
            return _status(raw.data)
        case "log":
            return _log(raw.data)
        case "output":
            model = LowOutput.model_validate(raw.data)
            return OutputReady(output=output_binder(model))
        case _:
            return None
