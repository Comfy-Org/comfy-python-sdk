"""Comfy SDK — the idiomatic Python client for the Comfy API v2.

The thick, hand-written layer integrators import. It runs an API-format workflow
against any Comfy API v2 surface (self-hosted proxy, Comfy Cloud, serverless) —
the only per-surface difference is the ``COMFY_BASE_URL`` environment variable
and an optional key — and owns
everything a generator cannot produce: local blake3 dedup-upload, ``core/ASSET``
substitution, idempotent submit, live SSE with a poll-authoritative backstop,
range-aware downloads, and typed errors. It is layered over ``comfy_low`` (the
generated protocol bindings + thin transport).

Quickstart::

    from comfy_sdk import Comfy

    client = Comfy(api_key="comfyui-...")              # Comfy Cloud
    # export COMFY_API_KEY=comfyui-...                 # ...or from the environment
    # client = Comfy()                                 # explicit key wins over it
    # export COMFY_BASE_URL=http://127.0.0.1:8189      # self-hosted, no key needed

    wf = client.workflows.from_file("workflow_api.json")
    asset = client.assets.from_file("photo.png")       # lazy; uploaded on use
    wf.set_input("10", "image", asset)

    result = client.run(wf)                            # submit + poll-to-done
    result.get_outputs("13")[0].to_file("out.png")
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .assets import Asset, AssetFactory, AsyncAsset, AsyncAssetFactory
from .client import (
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    COMFY_CLOUD_BASE_URL,
    COMFY_ROUTER_BASE_URL,
    ROUTER_BASE_URL_ENV_VAR,
    AsyncComfy,
    Comfy,
)
from .events import (
    Event,
    Log,
    OutputReady,
    Preview,
    Progress,
    StatusChange,
)
from .exceptions import (
    BlobNotFound,
    ComfyError,
    Forbidden,
    HashMismatch,
    IdempotencyKeyReuse,
    InsufficientCredits,
    InvalidWorkflow,
    JobFailed,
    MissingApiKey,
    MissingAsset,
    NotFound,
    QueueFull,
    Unauthorized,
    WorkflowFormatUi,
)
from .jobs import AsyncJob, Job, JobLogs, JobWorkflow
from .outputs import AsyncOutput, DownloadUrl, Output
from .retry import DEFAULT_RETRY, NO_RETRY, RetryPolicy
from .workflows import Workflow, WorkflowFactory

try:
    # Single source of truth is the installed distribution metadata, which
    # publish.yml stamps from the release tag. Hardcoding it here would pin
    # __version__ to the pyproject placeholder on every published release.
    __version__ = _pkg_version("comfy-sdk")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0+unknown"

__all__ = [
    # clients
    "Comfy",
    "COMFY_CLOUD_BASE_URL",
    "BASE_URL_ENV_VAR",
    "COMFY_ROUTER_BASE_URL",
    "ROUTER_BASE_URL_ENV_VAR",
    "API_KEY_ENV_VAR",
    "AsyncComfy",
    # assets / workflows / jobs / outputs
    "Asset",
    "AsyncAsset",
    "AssetFactory",
    "AsyncAssetFactory",
    "Workflow",
    "WorkflowFactory",
    "Job",
    "AsyncJob",
    "JobLogs",
    "JobWorkflow",
    "Output",
    "AsyncOutput",
    "DownloadUrl",
    # events
    "Event",
    "Progress",
    "Preview",
    "OutputReady",
    "StatusChange",
    "Log",
    # exceptions
    "ComfyError",
    "JobFailed",
    "QueueFull",
    "MissingAsset",
    "HashMismatch",
    "InvalidWorkflow",
    "WorkflowFormatUi",
    "BlobNotFound",
    "IdempotencyKeyReuse",
    "InsufficientCredits",
    "NotFound",
    "MissingApiKey",
    "Unauthorized",
    "Forbidden",
    # retry policy
    "RetryPolicy",
    "DEFAULT_RETRY",
    "NO_RETRY",
]
