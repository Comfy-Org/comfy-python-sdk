"""Typed-event construction for the event kinds the stub server never emits.

The stub only drives progress/preview/output/status frames with well-formed
data; the `log` event type and the preview base64-decode guard had no coverage.
"""

from __future__ import annotations

from comfy_low.sse import RawEvent
from comfy_sdk.events import Log, Preview, Progress, event_from_raw, progress_from_model

#: A progress payload with every optional field of the schema present.
_PROGRESS_WIRE = {
    "value": 0.42,
    "nodes_done": 11,
    "nodes_total": 31,
    "current_node": "12",
    "current_node_class": "KSampler",
    "step": 21,
    "steps": 50,
    "message": "KSampler 21/50",
}


def _binder(model):  # only OutputReady needs a real binder; unused here
    raise AssertionError("output_binder should not be called for these events")


def test_log_event_decodes():
    ev = event_from_raw(RawEvent(event="log", data={"level": "warn", "message": "x"}), _binder)
    assert isinstance(ev, Log)
    assert ev.level == "warn" and ev.message == "x"


def test_log_event_defaults_missing_fields():
    ev = event_from_raw(RawEvent(event="log", data={}), _binder)
    assert isinstance(ev, Log)
    assert ev.level == "info" and ev.message == ""


def test_preview_survives_undecodable_base64():
    # An unpadded/invalid base64 payload must not raise — data falls back to b"".
    ev = event_from_raw(
        RawEvent(event="preview", data={"data_base64": "abcde", "content_type": "image/png"}),
        _binder,
    )
    assert isinstance(ev, Preview)
    assert ev.data == b""
    assert ev.content_type == "image/png"


def test_unknown_event_name_is_skipped():
    assert event_from_raw(RawEvent(event="mystery", data={}), _binder) is None


def test_progress_event_carries_every_field_of_the_schema():
    # `current_node_class` was the one field of the progress schema the
    # decoder dropped. The stub server's frames do not send it, so this is
    # where it is pinned.
    ev = event_from_raw(RawEvent(event="progress", data=dict(_PROGRESS_WIRE)), _binder)
    assert ev == Progress(
        value=0.42,
        message="KSampler 21/50",
        nodes_done=11,
        nodes_total=31,
        current_node="12",
        step=21,
        steps=50,
        current_node_class="KSampler",
    )


def test_progress_from_model_matches_the_stream_decoder():
    # `job.progress` lifts the generated model; `job.events()` decodes the SSE
    # frame. Both produce the SDK's `Progress`, and the contract serves the
    # same schema down both paths — so for the same payload they must agree,
    # field for field. A field added to one lift and not the other fails here.
    from comfy_low.models import Progress as LowProgress

    from_stream = event_from_raw(RawEvent(event="progress", data=dict(_PROGRESS_WIRE)), _binder)
    from_model = progress_from_model(LowProgress.model_validate(dict(_PROGRESS_WIRE)))
    assert from_model == from_stream
