"""Built-in example workflows: something to run before you have a workflow of your own.

``client.workflows.from_example("text-to-image", prompt="...")`` returns an
ordinary :class:`~comfy_sdk.workflows.Workflow`, so everything that works on a
workflow you exported yourself works on one of these too (``set_input``,
``wf.json``, ``client.run``).

The graphs use models from the Comfy Cloud model library. A self-hosted
ComfyUI runs them only if it has the same model files installed.

Each example names its inputs (``prompt``, ``seed``, ``image``, ...) so a caller
never has to know which node id holds which field. ``examples()`` lists them.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .workflows import Workflow

_MAX_SEED = 2**32


@dataclass(frozen=True)
class ExampleInput:
    """One named input of an example: where it lands in the graph, and its default."""

    name: str
    node_id: str
    field: str
    description: str
    default: Any = None
    required: bool = False
    #: Builds a fresh default per call (the seed), so two runs do not repeat.
    default_factory: Callable[[], Any] | None = None

    def default_value(self) -> Any:
        return self.default_factory() if self.default_factory is not None else self.default


@dataclass(frozen=True)
class Example:
    """A built-in example workflow. List them with ``client.workflows.examples()``."""

    name: str
    description: str
    #: What the job produces: ``"image"`` or ``"video"``.
    output: str
    inputs: tuple[ExampleInput, ...]
    graph: dict[str, Any] = field(repr=False)
    aliases: tuple[str, ...] = ()

    def build(self, **values: Any) -> Workflow:
        """A fresh :class:`Workflow` with ``values`` applied over the defaults."""
        known = {i.name: i for i in self.inputs}
        unknown = sorted(set(values) - set(known))
        if unknown:
            raise ValueError(
                f"example {self.name!r} has no input {', '.join(map(repr, unknown))}; "
                f"its inputs are: {', '.join(known)}"
            )
        wf = Workflow(copy.deepcopy(self.graph))
        for spec in self.inputs:
            if spec.name in values:
                value = values[spec.name]
            elif spec.required:
                raise ValueError(
                    f"example {self.name!r} needs {spec.name}=... ({spec.description})"
                )
            else:
                value = spec.default_value()
            wf.set_input(spec.node_id, spec.field, value)
        return wf


def _random_seed() -> int:
    return secrets.randbelow(_MAX_SEED)


def _seed(node_id: str) -> ExampleInput:
    return ExampleInput(
        "seed",
        node_id,
        "seed",
        "random seed; pass one to make a run repeatable",
        default_factory=_random_seed,
    )


# Z-Image Turbo: 8 steps, good prompt adherence, a few seconds on Cloud.
_TEXT_TO_IMAGE_GRAPH: dict[str, Any] = {
    "1": {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"},
    },
    "2": {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"},
    },
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
    "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
    "6": {
        "class_type": "EmptySD3LatentImage",
        "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
    },
    "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
    "8": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["7", 0],
            "seed": 0,
            "steps": 8,
            "cfg": 1,
            "sampler_name": "res_multistep",
            "scheduler": "simple",
            "positive": ["4", 0],
            "negative": ["5", 0],
            "latent_image": ["6", 0],
            "denoise": 1,
        },
    },
    "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
    "10": {
        "class_type": "SaveImage",
        "inputs": {"images": ["9", 0], "filename_prefix": "comfy-sdk"},
    },
}

# Wan 2.2 TI2V 5B. The same graph does text-to-video when no start image is wired.
_WAN_VIDEO_GRAPH: dict[str, Any] = {
    "1": {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"},
    },
    "2": {
        "class_type": "CLIPLoader",
        "inputs": {
            "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "type": "wan",
            "device": "default",
        },
    },
    "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
    "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
    "7": {
        "class_type": "Wan22ImageToVideoLatent",
        "inputs": {"vae": ["3", 0], "width": 1280, "height": 704, "length": 73, "batch_size": 1},
    },
    "8": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8}},
    "9": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["8", 0],
            "seed": 0,
            "steps": 20,
            "cfg": 5,
            "sampler_name": "uni_pc",
            "scheduler": "simple",
            "positive": ["4", 0],
            "negative": ["5", 0],
            "latent_image": ["7", 0],
            "denoise": 1,
        },
    },
    "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
    "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 24}},
    "12": {
        "class_type": "SaveVideo",
        "inputs": {
            "video": ["11", 0],
            "filename_prefix": "comfy-sdk",
            "format": "auto",
            "codec": "auto",
        },
    },
}

_IMAGE_TO_VIDEO_GRAPH: dict[str, Any] = copy.deepcopy(_WAN_VIDEO_GRAPH)
_IMAGE_TO_VIDEO_GRAPH["6"] = {"class_type": "LoadImage", "inputs": {"image": ""}}
_IMAGE_TO_VIDEO_GRAPH["7"]["inputs"]["start_image"] = ["6", 0]

_NEGATIVE_VIDEO_PROMPT = "blurry, low quality, distorted, static, watermark, text"


def _video_inputs(prompt: str) -> tuple[ExampleInput, ...]:
    return (
        ExampleInput("prompt", "4", "text", "what should happen in the video", prompt),
        ExampleInput(
            "negative_prompt", "5", "text", "what to keep out of it", _NEGATIVE_VIDEO_PROMPT
        ),
        _seed("9"),
        ExampleInput("width", "7", "width", "frame width in pixels", 1280),
        ExampleInput("height", "7", "height", "frame height in pixels", 704),
        ExampleInput(
            "frames", "7", "length", "number of frames at 24 fps (4n+1, e.g. 49, 73, 121)", 73
        ),
    )


EXAMPLES: tuple[Example, ...] = (
    Example(
        name="text-to-image",
        description="Generate an image from a prompt (Z-Image Turbo, 1024x1024).",
        output="image",
        inputs=(
            ExampleInput(
                "prompt",
                "4",
                "text",
                "what to draw",
                "a lighthouse on a cliff at dusk, waves below, oil painting",
            ),
            _seed("8"),
            ExampleInput("width", "6", "width", "image width in pixels", 1024),
            ExampleInput("height", "6", "height", "image height in pixels", 1024),
        ),
        graph=_TEXT_TO_IMAGE_GRAPH,
        aliases=("txt2img", "t2i"),
    ),
    Example(
        name="image-to-video",
        description="Animate a still image into a 3 second clip (Wan 2.2 5B, 1280x704).",
        output="video",
        inputs=(
            ExampleInput(
                "image",
                "6",
                "image",
                "the first frame: an asset from client.assets.from_file('photo.png')",
                required=True,
            ),
            *_video_inputs("the camera slowly pushes in, gentle natural motion"),
        ),
        graph=_IMAGE_TO_VIDEO_GRAPH,
        aliases=("img2vid", "i2v"),
    ),
    Example(
        name="text-to-video",
        description="Generate a 3 second clip from a prompt (Wan 2.2 5B, 1280x704).",
        output="video",
        inputs=_video_inputs("a red fox trotting through fresh snow in a pine forest, morning"),
        graph=_WAN_VIDEO_GRAPH,
        aliases=("txt2vid", "t2v"),
    ),
)


def _key(name: str) -> str:
    return name.strip().lower().replace("_", "-")


_BY_NAME: dict[str, Example] = {
    _key(alias): ex for ex in EXAMPLES for alias in (ex.name, *ex.aliases)
}


def get_example(name: str) -> Example:
    """The example called ``name`` (case, ``_`` vs ``-`` and the short aliases are accepted)."""
    try:
        return _BY_NAME[_key(name)]
    except KeyError:
        names = ", ".join(ex.name for ex in EXAMPLES)
        raise ValueError(f"no example named {name!r}; available: {names}") from None
