"""Workflow construction/mutation and the sans-IO graph walk.

`Workflow.set_input` is the one call the module's own quickstart uses, yet had
no test; the graph-walk helpers were only exercised with an asset embedded as a
plain dict value, never nested in a list (batch inputs).
"""

from __future__ import annotations

from comfy_sdk._core import find_asset_handles, substitute_asset_handles
from comfy_sdk.workflows import Workflow, WorkflowFactory


class _FakeHandle:
    """Stands in for an Asset handle (detected via `_is_comfy_asset`)."""

    _is_comfy_asset = True


def test_set_input_sets_plain_value_and_creates_missing_node():
    wf = Workflow({"1": {"inputs": {}}})
    wf.set_input("1", "seed", 42)
    assert wf.json["1"]["inputs"]["seed"] == 42
    # A node/inputs dict that doesn't exist yet is created via setdefault.
    wf.set_input("3", "cfg", 7.5)
    assert wf.json["3"]["inputs"]["cfg"] == 7.5


def test_set_input_embeds_an_asset_handle_verbatim():
    handle = _FakeHandle()
    wf = Workflow({})
    wf.set_input("10", "image", handle)
    assert wf.json["10"]["inputs"]["image"] is handle  # substituted only at submit


def test_workflow_factory_from_str_and_from_json():
    wf = WorkflowFactory().from_str('{"1": {"class_type": "X"}}')
    assert wf.json == {"1": {"class_type": "X"}}
    graph = {"2": {"class_type": "Y"}}
    assert WorkflowFactory().from_json(graph).json is graph


def test_find_and_substitute_handles_nested_in_a_list():
    h1, h2 = _FakeHandle(), _FakeHandle()
    graph = {"1": {"inputs": {"images": [h1, h2, "literal"]}}}
    found = find_asset_handles(graph)
    assert found == [h1, h2]
    refs = {
        id(h1): {"__type": "core/ASSET", "info": {"id": "a"}},
        id(h2): {"__type": "core/ASSET", "info": {"id": "b"}},
    }
    out = substitute_asset_handles(graph, refs)
    assert out == {
        "1": {
            "inputs": {
                "images": [
                    {"__type": "core/ASSET", "info": {"id": "a"}},
                    {"__type": "core/ASSET", "info": {"id": "b"}},
                    "literal",
                ]
            }
        }
    }


def test_plain_graph_passes_through_the_walk_unchanged():
    graph = {"1": {"inputs": {"seed": 42, "model": "x.safetensors"}}}
    assert find_asset_handles(graph) == []
    assert substitute_asset_handles(graph, {}) == graph


def test_add_node_redirects_downstream_single_consumer():
    graph = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "model.safetensors"},
        },
        "2": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": "lora.safetensors",
                "strength_model": 1,
                "model": ["1", 0],
            },
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 0,
                "model": ["2", 0],
            },
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "ModelAttentionBackend",
        before="4",
        inputs={
            "attention": "pytorch attention",
            "model": ["2", 0],
        },
    )
    assert new_id == "5"
    assert wf.json[new_id]["class_type"] == "ModelAttentionBackend"
    assert wf.json[new_id]["inputs"]["model"] == ["2", 0]
    assert wf.json["4"]["inputs"]["model"] == [new_id, 0]


def test_add_node_redirects_multiple_downstream():
    graph = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "example.png"},
        },
        "2": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
        "3": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
        "4": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "ImageScaleToTotalPixels",
        after="1",
        inputs={
            "upscale_method": "nearest-exact",
            "megapixels": 1,
            "image": ["1", 0],
        },
    )
    assert new_id == "5"
    assert wf.json[new_id]["inputs"]["image"] == ["1", 0]
    assert wf.json["2"]["inputs"]["images"] == [new_id, 0]
    assert wf.json["3"]["inputs"]["images"] == [new_id, 0]
    assert wf.json["4"]["inputs"]["images"] == [new_id, 0]


def test_add_node_no_redirect_when_upstream_not_in_graph():
    graph = {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "hello"},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "KSampler",
        inputs={"model": ["999", 0]},
    )
    assert new_id == "2"
    assert wf.json[new_id]["inputs"]["model"] == ["999", 0]


def test_add_node_no_redirect_when_no_downstream_consumers():
    graph = {
        "1": {
            "class_type": "CheckpointLoader",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "LoraLoader",
        inputs={"model": ["1", 0], "clip": ["1", 1]},
    )
    assert new_id == "2"
    assert wf.json[new_id]["inputs"]["model"] == ["1", 0]
    assert wf.json[new_id]["inputs"]["clip"] == ["1", 1]


def test_add_node_both_before_and_after_raises():
    wf = Workflow({"1": {"class_type": "X", "inputs": {}}})
    try:
        wf.add_node("Y", before="1", after="1", inputs={})
    except ValueError as e:
        assert "not both" in str(e)
    else:
        assert False, "Expected ValueError"


def test_add_node_no_inputs_no_redirect():
    wf = Workflow({"1": {"class_type": "X", "inputs": {}}})
    new_id = wf.add_node("Y")
    assert new_id == "2"
    assert wf.json[new_id]["class_type"] == "Y"
    assert "inputs" not in wf.json[new_id]


def test_add_node_auto_id_on_empty_graph():
    wf = Workflow({})
    new_id = wf.add_node("X")
    assert new_id == "1"


def test_add_node_redirects_downstream_single_consumer():
    graph = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "model.safetensors"},
        },
        "2": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": "lora.safetensors",
                "strength_model": 1,
                "model": ["1", 0],
            },
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 0,
                "model": ["2", 0],
            },
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "ModelAttentionBackend",
        before="4",
        inputs={
            "attention": "pytorch attention",
            "model": ["2", 0],
        },
    )
    assert new_id == "5"
    assert wf.json[new_id]["class_type"] == "ModelAttentionBackend"
    assert wf.json[new_id]["inputs"]["model"] == ["2", 0]
    assert wf.json["4"]["inputs"]["model"] == [new_id, 0]


def test_add_node_redirects_multiple_downstream():
    graph = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "example.png"},
        },
        "2": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
        "3": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
        "4": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "ImageScaleToTotalPixels",
        after="1",
        inputs={
            "upscale_method": "nearest-exact",
            "megapixels": 1,
            "image": ["1", 0],
        },
    )
    assert new_id == "5"
    assert wf.json[new_id]["inputs"]["image"] == ["1", 0]
    assert wf.json["2"]["inputs"]["images"] == [new_id, 0]
    assert wf.json["3"]["inputs"]["images"] == [new_id, 0]
    assert wf.json["4"]["inputs"]["images"] == [new_id, 0]


def test_add_node_no_redirect_when_upstream_not_in_graph():
    graph = {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "hello"},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "KSampler",
        inputs={"model": ["999", 0]},
    )
    assert new_id == "2"
    assert wf.json[new_id]["inputs"]["model"] == ["999", 0]


def test_add_node_no_redirect_when_no_downstream_consumers():
    graph = {
        "1": {
            "class_type": "CheckpointLoader",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "LoraLoader",
        inputs={"model": ["1", 0], "clip": ["1", 1]},
    )
    assert new_id == "2"
    assert wf.json[new_id]["inputs"]["model"] == ["1", 0]
    assert wf.json[new_id]["inputs"]["clip"] == ["1", 1]


def test_add_node_both_before_and_after_raises():
    wf = Workflow({"1": {"class_type": "X", "inputs": {}}})
    try:
        wf.add_node("Y", before="1", after="1", inputs={})
    except ValueError as e:
        assert "not both" in str(e)
    else:
        assert False, "Expected ValueError"


def test_add_node_no_inputs_no_redirect():
    wf = Workflow({"1": {"class_type": "X", "inputs": {}}})
    new_id = wf.add_node("Y")
    assert new_id == "2"
    assert wf.json[new_id]["class_type"] == "Y"
    assert "inputs" not in wf.json[new_id]


def test_add_node_auto_id_on_empty_graph():
    wf = Workflow({})
    new_id = wf.add_node("X")
    assert new_id == "1"


def test_remove_node_model_attention_backend():
    graph = {
        "1": {
            "inputs": {
                "unet_name": "z.safetensors",
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
            "_meta": {"title": "Load Diffusion Model"},
        },
        "2": {
            "inputs": {
                "lora_name": "e.safetensors",
                "strength_model": 1,
                "model": ["1", 0],
            },
            "class_type": "LoraLoaderModelOnly",
            "_meta": {"title": "Load LoRA"},
        },
        "3": {
            "inputs": {
                "attention": "pytorch attention",
                "model": ["2", 0],
            },
            "class_type": "ModelAttentionBackend",
            "_meta": {"title": "ModelAttentionBackend"},
        },
        "4": {
            "inputs": {
                "seed": 0,
                "steps": 20,
                "cfg": 8,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1,
                "model": ["3", 0],
            },
            "class_type": "KSampler",
            "_meta": {"title": "KSampler"},
        },
    }
    wf = Workflow(graph)
    wf.remove_node("3")

    assert "3" not in wf.json
    assert wf.json["4"]["inputs"]["model"] == ["2", 0]


def test_remove_node_redirects_image_scale():
    graph = {
        "1": {
            "inputs": {"image": "example.png"},
            "class_type": "LoadImage",
            "_meta": {"title": "Load Image (A)"},
        },
        "2": {
            "inputs": {"images": ["5", 0]},
            "class_type": "PreviewImage",
            "_meta": {"title": "Preview Image (B)"},
        },
        "3": {
            "inputs": {"images": ["5", 0]},
            "class_type": "PreviewImage",
            "_meta": {"title": "Preview Image (C)"},
        },
        "4": {
            "inputs": {"images": ["5", 0]},
            "class_type": "PreviewImage",
            "_meta": {"title": "Preview Image (D)"},
        },
        "5": {
            "inputs": {
                "upscale_method": "nearest-exact",
                "megapixels": 1,
                "resolution_steps": 1,
                "image": ["1", 0],
            },
            "class_type": "ImageScaleToTotalPixels",
            "_meta": {"title": "Scale Image to Total Pixels (E)"},
        },
    }
    wf = Workflow(graph)
    wf.remove_node("5")

    assert "5" not in wf.json
    assert wf.json["2"]["inputs"]["images"] == ["1", 0]
    assert wf.json["3"]["inputs"]["images"] == ["1", 0]
    assert wf.json["4"]["inputs"]["images"] == ["1", 0]


def test_remove_node_redirects_preview_any():
    graph = {
        "1": {
            "inputs": {
                "prompt": "test",
                "max_length": 512,
                "sampling_mode": "on",
                "sampling_mode.temperature": 0.7,
                "sampling_mode.top_k": 64,
                "sampling_mode.top_p": 0.95,
                "sampling_mode.min_p": 0.05,
                "sampling_mode.repetition_penalty": 1.05,
                "sampling_mode.seed": 0,
                "sampling_mode.presence_penalty": 0,
                "thinking": False,
                "use_default_template": True,
                "clip": ["4", 0],
            },
            "class_type": "TextGenerate",
            "_meta": {"title": "Generate Text"},
        },
        "2": {
            "inputs": {"source": ["1", 0]},
            "class_type": "PreviewAny",
            "_meta": {"title": "Preview as Text"},
        },
        "3": {
            "inputs": {
                "filename_prefix": "ComfyUI",
                "format": "txt",
                "text": ["2", 0],
            },
            "class_type": "SaveText",
            "_meta": {"title": "Save Text"},
        },
        "4": {
            "inputs": {
                "clip_name": "qwen3vl_4b_bf16.safetensors",
                "type": "stable_diffusion",
                "device": "default",
            },
            "class_type": "CLIPLoader",
            "_meta": {"title": "Load CLIP"},
        },
    }
    wf = Workflow(graph)
    wf.remove_node("2")

    assert "2" not in wf.json
    assert wf.json["3"]["inputs"]["text"] == ["1", 0]


def test_add_node_redirects_downstream_single_consumer():
    graph = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "model.safetensors"},
        },
        "2": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": "lora.safetensors",
                "strength_model": 1,
                "model": ["1", 0],
            },
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 0,
                "model": ["2", 0],
            },
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "ModelAttentionBackend",
        before="4",
        inputs={
            "attention": "pytorch attention",
            "model": ["2", 0],
        },
    )
    assert new_id == "5"
    assert wf.json[new_id]["class_type"] == "ModelAttentionBackend"
    assert wf.json[new_id]["inputs"]["model"] == ["2", 0]
    assert wf.json["4"]["inputs"]["model"] == [new_id, 0]


def test_add_node_redirects_multiple_downstream():
    graph = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": "example.png"},
        },
        "2": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
        "3": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
        "4": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["1", 0]},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "ImageScaleToTotalPixels",
        after="1",
        inputs={
            "upscale_method": "nearest-exact",
            "megapixels": 1,
            "image": ["1", 0],
        },
    )
    assert new_id == "5"
    assert wf.json[new_id]["inputs"]["image"] == ["1", 0]
    assert wf.json["2"]["inputs"]["images"] == [new_id, 0]
    assert wf.json["3"]["inputs"]["images"] == [new_id, 0]
    assert wf.json["4"]["inputs"]["images"] == [new_id, 0]


def test_add_node_no_redirect_when_upstream_not_in_graph():
    graph = {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "hello"},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "KSampler",
        inputs={"model": ["999", 0]},
    )
    assert new_id == "2"
    assert wf.json[new_id]["inputs"]["model"] == ["999", 0]


def test_add_node_no_redirect_when_no_downstream_consumers():
    graph = {
        "1": {
            "class_type": "CheckpointLoader",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
    }
    wf = Workflow(graph)
    new_id = wf.add_node(
        "LoraLoader",
        inputs={"model": ["1", 0], "clip": ["1", 1]},
    )
    assert new_id == "2"
    assert wf.json[new_id]["inputs"]["model"] == ["1", 0]
    assert wf.json[new_id]["inputs"]["clip"] == ["1", 1]


def test_add_node_both_before_and_after_raises():
    wf = Workflow({"1": {"class_type": "X", "inputs": {}}})
    try:
        wf.add_node("Y", before="1", after="1", inputs={})
    except ValueError as e:
        assert "not both" in str(e)
    else:
        assert False, "Expected ValueError"


def test_add_node_no_inputs_no_redirect():
    wf = Workflow({"1": {"class_type": "X", "inputs": {}}})
    new_id = wf.add_node("Y")
    assert new_id == "2"
    assert wf.json[new_id]["class_type"] == "Y"
    assert "inputs" not in wf.json[new_id]


def test_add_node_auto_id_on_empty_graph():
    wf = Workflow({})
    new_id = wf.add_node("X")
    assert new_id == "1"
