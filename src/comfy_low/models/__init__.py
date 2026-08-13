"""Generated pydantic v2 models for the Comfy API v2 contract.

Everything here is emitted from ``spec/openapi.yaml`` by ``scripts/gen_models.sh``
and re-exported for convenience. Do not hand-edit ``_generated.py``.

Note on naming: request bodies in the canonical spec are inlined (not named
schemas), so there are no ``*Request`` / ``*Form`` models — the transport builds
those payloads by hand. The SSE event payloads are ``StatusEvent`` /
``PreviewEvent`` / ``LogEvent`` (``progress`` and ``output`` events reuse the
``Progress`` and ``Output`` schemas directly).
"""

from __future__ import annotations

from ._generated import (
    Asset,
    AssetReference,
    Error,
    ErrorEnvelope,
    Format,
    Job,
    JobError,
    JobStatus,
    JobUrls,
    JobWorkflowResponse,
    LogEvent,
    Output,
    OutputType,
    PreviewEvent,
    Progress,
    StatusEvent,
)

__all__ = [
    "Asset",
    "AssetReference",
    "Error",
    "ErrorEnvelope",
    "Format",
    "Job",
    "JobError",
    "JobStatus",
    "JobUrls",
    "JobWorkflowResponse",
    "LogEvent",
    "Output",
    "OutputType",
    "PreviewEvent",
    "Progress",
    "StatusEvent",
]
