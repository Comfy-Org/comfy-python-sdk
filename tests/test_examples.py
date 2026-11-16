"""Built-in example workflows (``client.workflows.from_example``).

The graphs are data, so the risk is drift: an input that names a node or field
the graph no longer has, or a link to a node that is gone. Those are checked for
every example rather than for the ones someone remembered to list.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfy_sdk import Comfy, Example
from comfy_sdk._core import find_asset_handles
from comfy_sdk.examples import _MAX_SEED, EXAMPLES, get_example
from comfy_sdk.workflows import WorkflowFactory

_FACTORY = WorkflowFactory()


class _FakeHandle:
    _is_comfy_asset = True


def _required(example: Example) -> dict[str, Any]:
    return {i.name: _FakeHandle() for i in example.inputs if i.required}


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_every_input_lands_on_an_existing_node_and_field(example: Example) -> None:
    for spec in example.inputs:
        node = example.graph.get(spec.node_id)
        assert node is not None, f"{example.name}.{spec.name} names missing node {spec.node_id}"
        assert spec.field in node["inputs"], f"{example.name}.{spec.name}: no field {spec.field}"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_every_link_points_at_a_node_in_the_graph(example: Example) -> None:
    wf = example.build(**_required(example))
    for node_id, node in wf.json.items():
        assert "class_type" in node, node_id
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in wf.json, f"{example.name}: {node_id} links to {value[0]}"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_ends_in_a_save_node_for_its_output_kind(example: Example) -> None:
    savers = {n["class_type"] for n in example.graph.values() if n["class_type"].startswith("Save")}
    assert savers == {"SaveImage" if example.output == "image" else "SaveVideo"}


def test_named_inputs_are_applied() -> None:
    wf = _FACTORY.from_example("text-to-image", prompt="a red fox", seed=7, width=768)
    assert wf.json["4"]["inputs"]["text"] == "a red fox"
    assert wf.json["8"]["inputs"]["seed"] == 7
    assert wf.json["6"]["inputs"]["width"] == 768
    assert wf.json["6"]["inputs"]["height"] == 1024


def test_defaults_fill_every_input_and_the_seed_is_drawn_per_call() -> None:
    a = _FACTORY.from_example("text-to-image")
    assert a.json["4"]["inputs"]["text"]
    seeds = {_FACTORY.from_example("text-to-image").json["8"]["inputs"]["seed"] for _ in range(8)}
    assert all(isinstance(s, int) and 0 <= s < _MAX_SEED for s in seeds)
    assert len(seeds) > 1


def test_building_never_mutates_the_template() -> None:
    before = repr(get_example("text-to-image").graph)
    wf = _FACTORY.from_example("text-to-image", prompt="changed")
    wf.json["1"]["inputs"]["unet_name"] = "other"
    assert repr(get_example("text-to-image").graph) == before


@pytest.mark.parametrize("name", ["text-to-image", "TEXT_TO_IMAGE", " txt2img ", "t2i"])
def test_names_are_forgiving(name: str) -> None:
    assert get_example(name).name == "text-to-image"


def test_unknown_example_lists_the_available_ones() -> None:
    with pytest.raises(ValueError, match="text-to-image, image-to-video, text-to-video"):
        _FACTORY.from_example("txt2music")


def test_unknown_input_lists_the_valid_ones() -> None:
    with pytest.raises(ValueError, match="no input 'promt'.*prompt, seed, width, height"):
        _FACTORY.from_example("text-to-image", promt="typo")


def test_missing_required_image_says_how_to_get_one() -> None:
    with pytest.raises(ValueError, match=r"needs image=.*client\.assets\.from_file"):
        _FACTORY.from_example("image-to-video")


def test_text_to_video_has_no_image_input() -> None:
    wf = _FACTORY.from_example("text-to-video")
    assert "start_image" not in wf.json["7"]["inputs"]
    assert all(n["class_type"] != "LoadImage" for n in wf.json.values())


def test_image_handle_is_embedded_for_submit() -> None:
    handle = _FakeHandle()
    wf = _FACTORY.from_example("image-to-video", image=handle)
    assert find_asset_handles(wf.json) == [handle]


def test_examples_lists_every_example() -> None:
    assert [e.name for e in _FACTORY.examples()] == [e.name for e in EXAMPLES]


def test_an_example_submits_as_an_ordinary_workflow(server) -> None:
    with Comfy() as client:
        wf = client.workflows.from_example("text-to-image", prompt="hello", seed=1)
        client.submit(wf)
    sent = server.state.last_workflow
    assert sent["4"]["inputs"]["text"] == "hello"
    assert sent["8"]["inputs"]["seed"] == 1
