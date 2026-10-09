"""Live end-to-end test of the Router model-run surface (``client.models.run``)
against a deployment, exercising the alt-provider controls (``model_provider``,
``strict_mode``, ``fallback_provider``).

Skipped unless pointed at a live Router deployment:

    export COMFY_ROUTER_BASE_URL="https://stagingapi.comfy.org"
    export COMFY_API_KEY="comfyui-..."
    pytest tests/integration/test_router_models_e2e.py -v

Gated on COMFY_ROUTER_BASE_URL being set explicitly (not just COMFY_API_KEY):
these calls dispatch real partner generations and cost credits, so they run
only against a deployment the caller deliberately named, never accidentally
against the default prod host. The provider under test defaults to ``fal`` and
the model ids to fal's registered alt-provider legs; override with
COMFY_ROUTER_E2E_PROVIDER / _IMAGE_MODEL / _VIDEO_MODEL to point elsewhere.

The provider gate must be enabled for the caller on the target deployment, or
these are refused ``not_enabled`` (that refusal is itself the signal the gate
is off, not an SDK fault).
"""

from __future__ import annotations

import os

import pytest

from comfy_sdk import ROUTER_BASE_URL_ENV_VAR, AsyncComfy, Comfy
from comfy_sdk.router_exceptions import InvalidInput, NotEnabled

ROUTER_BASE_URL = os.environ.get(ROUTER_BASE_URL_ENV_VAR)
API_KEY = os.environ.get("COMFY_API_KEY")
PROVIDER = os.environ.get("COMFY_ROUTER_E2E_PROVIDER", "fal")
# Native model ids that carry an alt-provider leg — the {provider}/{model} the
# run route is addressed by, not the alt-provider's own catalog id.
IMAGE_MODEL = os.environ.get("COMFY_ROUTER_E2E_IMAGE_MODEL", "openai/gpt-image-2")
VIDEO_MODEL = os.environ.get(
    "COMFY_ROUTER_E2E_VIDEO_MODEL", "byteplus/dreamina-seedance-2-0-260128"
)
# Wavespeed is a SECOND alt-provider for nano-banana-pro, text-to-image only and
# the native Gemini generateContent shape — so it needs its own model id + body
# rather than the gpt-image-2-shaped IMAGE_MODEL above.
WAVESPEED_MODEL = os.environ.get(
    "COMFY_ROUTER_E2E_WAVESPEED_MODEL", "vertexai/gemini-3-pro-image"
)
RUN_TIMEOUT_S = 300  # a direct image generation, held server-side
VIDEO_TIMEOUT_S = 600  # submit-poll video, polled server-side inside the call

#: These calls BILL, so the opt-in is a DEDICATED variable rather than the SDK's
#: own documented credentials. `COMFY_ROUTER_BASE_URL` + `COMFY_API_KEY` are
#: exactly what a developer pointed at staging has exported already, so gating
#: on those alone means a plain `pytest` run silently spends money on an image
#: AND a video generation. `COMFY_ROUTER_E2E=1` is the same opt-in the cloud
#: repo's Router e2e suite uses, so the two agree on what "yes, bill me" means.
E2E_OPT_IN = os.environ.get("COMFY_ROUTER_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not (E2E_OPT_IN and ROUTER_BASE_URL and API_KEY),
    reason=(
        "set COMFY_ROUTER_E2E=1 (these calls bill) plus COMFY_ROUTER_BASE_URL "
        "and COMFY_API_KEY to run Router model e2e tests"
    ),
)


@pytest.fixture(scope="module")
def client() -> Comfy:
    c = Comfy(api_key=API_KEY)
    yield c
    c.close()


def _image_url(out: dict) -> str:
    """The image URL/data-URI from a native OpenAI-image response, or ''."""
    data = out.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("url") or data[0].get("b64_json") or ""
    return ""


def test_alt_provider_translates_native_round_trip(client: Comfy) -> None:
    """model_provider=<name>, strict_mode default: native input in, native out.

    The caller sends the model's OWN native contract and gets the model's own
    native output back, with the alt-provider translation invisible — the whole
    point of the default (strict_mode=false) path.
    """
    out = client.models.run(
        IMAGE_MODEL,
        {"prompt": "a red fox in a snowy forest", "n": 1, "size": "1024x1024"},
        model_provider=PROVIDER,
        timeout=RUN_TIMEOUT_S,
    )
    assert isinstance(out, dict), f"expected a native dict, got {type(out).__name__}"
    assert _image_url(out), f"no image in native round-trip response: keys={list(out)}"


def test_strict_mode_returns_the_providers_raw_shape(client: Comfy) -> None:
    """strict_mode=true: the body is the provider's OWN shape, passed through,
    and the response is the provider's raw shape — no native translation."""
    out = client.models.run(
        IMAGE_MODEL,
        {"prompt": "a red fox, oil painting", "image_size": {"width": 1024, "height": 1024}},
        model_provider=PROVIDER,
        strict_mode=True,
        timeout=RUN_TIMEOUT_S,
    )
    assert isinstance(out, dict) and out, f"empty strict-mode response: {out!r}"
    # The provider's own shape, not the native `data[]` envelope.
    assert "data" not in out or "images" in out, (
        f"strict_mode response looks native, expected the provider's raw shape: keys={list(out)}"
    )


def test_fallback_provider_opt_out_is_accepted(client: Comfy) -> None:
    """fallback_provider='false' is accepted and the call still succeeds; it
    only changes behavior on a primary failure, so on success it is a no-op."""
    out = client.models.run(
        IMAGE_MODEL,
        {"prompt": "a red fox, watercolor", "n": 1, "size": "1024x1024"},
        model_provider=PROVIDER,
        fallback_provider="false",
        timeout=RUN_TIMEOUT_S,
    )
    assert _image_url(out), f"no image with fallback_provider=false: keys={list(out)}"


async def test_alt_provider_on_the_async_client(client: Comfy) -> None:
    """The awaitable form carries the same params to the same result shape."""
    async with AsyncComfy(api_key=API_KEY) as ac:
        out = await ac.models.run(
            IMAGE_MODEL,
            {"prompt": "a red fox, pixel art", "n": 1, "size": "1024x1024"},
            model_provider=PROVIDER,
            timeout=RUN_TIMEOUT_S,
        )
    assert _image_url(out), f"no image from async alt-provider run: keys={list(out)}"


def test_submit_poll_model_via_alt_provider(client: Comfy) -> None:
    """A submit-and-poll (video) model served via the alt provider: run() blocks
    while the server polls to completion, and the native terminal shape comes
    back. This is the second dispatch mode (the image models above are direct)."""
    out = client.models.run(
        VIDEO_MODEL,
        {
            "content": [{"type": "text", "text": "a red fox running through a snowy forest"}],
            "resolution": "480p",
            "ratio": "16:9",
            "duration": 5,
        },
        model_provider=PROVIDER,
        timeout=VIDEO_TIMEOUT_S,
    )
    assert isinstance(out, dict) and out, f"empty video response: {out!r}"
    content = out.get("content") if isinstance(out.get("content"), dict) else {}
    assert out.get("status") == "succeeded", f"video not succeeded: {out.get('status')!r}"
    assert content.get("video_url"), f"no video_url in terminal response: keys={list(out)}"


def test_second_alt_provider_wavespeed_nano_banana_pro(client: Comfy) -> None:
    """A model with more than one registered provider: model_provider=wavespeed
    on nano-banana-pro (native Gemini generateContent, text-to-image only).

    Skipped (not failed) when wavespeed is not yet deployed or its gate is off
    on this deployment, so the suite stays green while the provider ramps.
    """
    try:
        out = client.models.run(
            WAVESPEED_MODEL,
            {"contents": [{"role": "user", "parts": [{"text": "a red fox in a snowy forest"}]}]},
            model_provider="wavespeed",
            timeout=RUN_TIMEOUT_S,
        )
    except InvalidInput as exc:  # model_provider not recognized -> not deployed here yet
        pytest.skip(f"wavespeed not registered on this deployment yet: {exc}")
    except NotEnabled as exc:  # gate off for this caller on this deployment
        pytest.skip(f"wavespeed gate not enabled on this deployment: {exc}")
    assert isinstance(out, dict) and out, f"empty wavespeed response: {out!r}"
    cands = out.get("candidates")
    assert isinstance(cands, list) and cands, (
        f"no candidates in native Gemini response: keys={list(out)}"
    )
