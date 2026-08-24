"""Sync/async public-surface parity across the 8 mirrored class pairs.

The README promises "swap the import and add ``await``" as the only
difference; this asserts the public method names actually match. Regression
guard for ``AsyncOutput`` shipping without ``to_stream``.

Dunders (``__enter__``/``__aenter__``, ...) are excluded — they're a mechanical
consequence of sync vs async, not part of the call surface a swap has to
match. The intentional ``close``/``aclose`` rename is normalised away.
"""

from __future__ import annotations

import pytest

from comfy_low.transport import AsyncComfyLow, ComfyLow
from comfy_sdk.assets import Asset, AssetFactory, AsyncAsset, AsyncAssetFactory
from comfy_sdk.client import AsyncComfy, Comfy
from comfy_sdk.jobs import AsyncJob, AsyncJobFactory, Job, JobFactory
from comfy_sdk.models import AsyncModels, Models
from comfy_sdk.outputs import AsyncOutput, Output

_PAIRS: list[tuple[str, type, type]] = [
    ("Comfy", Comfy, AsyncComfy),
    ("Asset", Asset, AsyncAsset),
    ("AssetFactory", AssetFactory, AsyncAssetFactory),
    ("Job", Job, AsyncJob),
    ("JobFactory", JobFactory, AsyncJobFactory),
    ("Output", Output, AsyncOutput),
    ("Models", Models, AsyncModels),
    ("ComfyLow", ComfyLow, AsyncComfyLow),
]


def _public_names(cls: type) -> set[str]:
    names = {n for n in dir(cls) if not n.startswith("_")}
    return {"close" if n == "aclose" else n for n in names}


@pytest.mark.parametrize("label,sync_cls,async_cls", _PAIRS, ids=[p[0] for p in _PAIRS])
def test_sync_async_public_surface_matches(label: str, sync_cls: type, async_cls: type) -> None:
    sync_names = _public_names(sync_cls)
    async_names = _public_names(async_cls)
    sync_only = sync_names - async_names
    async_only = async_names - sync_names
    assert not sync_only and not async_only, (
        f"{label}/Async{label} public surface diverges: "
        f"sync-only={sorted(sync_only)} async-only={sorted(async_only)}"
    )
