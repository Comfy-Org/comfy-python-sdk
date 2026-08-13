"""``comfy_low`` — generated + thin-transport protocol bindings for Comfy API v2.

Two parts:

* ``comfy_low.models`` — pydantic v2 models generated from ``spec/openapi.yaml``
  (do not hand-edit; regenerate with ``scripts/gen_models.sh``).
* ``comfy_low.transport`` — a hand-written thin ``httpx`` transport (sync +
  async) with one function per ``operationId`` and the mandatory escape hatches
  (raw response, streaming bodies, all headers, per-request timeout/abort).

This layer is deliberately boring: no orchestration, retries, hashing, or SSE
reconnection. Those live in ``comfy_sdk``.
"""

from __future__ import annotations

from . import models
from .errors import (
    ApiError,
    BlobNotFound,
    Forbidden,
    HashMismatch,
    IdempotencyKeyReuse,
    InsufficientCredits,
    InvalidWorkflow,
    MissingAsset,
    NotFound,
    QueueFull,
    Unauthorized,
    WorkflowFormatUi,
    error_from_envelope,
)
from .sse import RawEvent, SSEDecoder
from .transport import AsyncComfyLow, ComfyLow

# The exact set of operationIds the transport must cover; the spec-coverage test
# asserts this equals the set of operationIds in spec/openapi.yaml.
OPERATION_IDS: frozenset[str] = frozenset(
    {
        "postAssets",
        "assetFromHash",
        "headAssetByHash",
        "getAsset",
        "deleteAsset",
        "getAssetContent",
        "postJobs",
        "getJob",
        "getJobWorkflow",
        "getJobEvents",
        "cancelJob",
    }
)

# operationId -> transport method name (same mapping for sync and async).
OPERATION_METHODS: dict[str, str] = {
    "postAssets": "post_assets",
    "assetFromHash": "asset_from_hash",
    "headAssetByHash": "head_asset_by_hash",
    "getAsset": "get_asset",
    "deleteAsset": "delete_asset",
    "getAssetContent": "get_asset_content",
    "postJobs": "post_jobs",
    "getJob": "get_job",
    "getJobWorkflow": "get_job_workflow",
    "getJobEvents": "get_job_events",
    "cancelJob": "cancel_job",
}

__all__ = [
    "models",
    "ComfyLow",
    "AsyncComfyLow",
    "RawEvent",
    "SSEDecoder",
    "ApiError",
    "InvalidWorkflow",
    "WorkflowFormatUi",
    "MissingAsset",
    "HashMismatch",
    "BlobNotFound",
    "IdempotencyKeyReuse",
    "QueueFull",
    "InsufficientCredits",
    "NotFound",
    "Unauthorized",
    "Forbidden",
    "error_from_envelope",
    "OPERATION_IDS",
    "OPERATION_METHODS",
]
