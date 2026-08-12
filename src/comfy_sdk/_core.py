"""Sans-IO core shared by the sync and async clients.

Everything here is pure decision logic — no network, no awaiting — so the sync
and async layers stay a thin IO shell over one implementation of the rules:
terminal-state detection, adaptive poll backoff, idempotency-key minting, the
``core/ASSET`` reference shape, and the workflow-graph walk that finds and
substitutes asset handles.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

# Terminal job states. ``canceling`` is deliberately NOT terminal — cancellation
# takes effect at node/step boundaries and can take seconds.
TERMINAL: frozenset[str] = frozenset({"succeeded", "canceled", "failed", "expired"})
SUCCESS = "succeeded"


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def extra_data_for(
    api_key: str | None, workflow_graph: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Build the wire ``extra_data`` object.

    ``api_key`` authenticates partner (API) nodes (e.g. Gemini) embedded in a
    workflow — it is unrelated to the client's own ``Authorization`` bearer
    token. ``workflow_graph``, when given, is embedded as
    ``extra_pnginfo.workflow`` — the same key local ComfyUI's ``SaveImage``
    writes into output PNG metadata, so the graph can be recovered from a
    generated image. Pass the caller's own materialized graph; this function
    does no materialization itself. Returns ``None`` when neither is supplied,
    so callers omit ``extra_data`` from the request entirely rather than
    sending an empty object.
    """
    data: dict[str, Any] = {}
    if api_key:
        data["api_key_comfy_org"] = api_key
    if workflow_graph is not None:
        data["extra_pnginfo"] = {"workflow": workflow_graph}
    return data or None


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def backoff_schedule(start: float = 0.5, factor: float = 1.5, cap: float = 5.0) -> Iterator[float]:
    """Adaptive poll intervals: start small, grow, then hold at ``cap``."""
    delay = start
    while True:
        yield delay
        delay = min(delay * factor, cap)


def asset_reference(
    asset_id: str, *, hash: str | None = None, file_path: str | None = None
) -> dict[str, Any]:
    """Build the ``core/ASSET`` object substituted into workflow JSON.

    ``id`` is authoritative and always present; ``hash`` and ``file_path`` are
    optional staging / cross-surface hints.
    """
    info: dict[str, Any] = {"id": asset_id}
    if hash is not None:
        info["hash"] = hash
    if file_path is not None:
        info["file_path"] = file_path
    return {"__type": "core/ASSET", "info": info}


def _is_asset_handle(value: Any) -> bool:
    return getattr(value, "_is_comfy_asset", False) is True


def find_asset_handles(graph: Any, _seen: set[int] | None = None) -> list[Any]:
    """Collect every asset handle embedded anywhere in the workflow graph."""
    found: list[Any] = []
    if _is_asset_handle(graph):
        found.append(graph)
        return found
    if isinstance(graph, dict):
        for v in graph.values():
            found.extend(find_asset_handles(v))
    elif isinstance(graph, (list, tuple)):
        for v in graph:
            found.extend(find_asset_handles(v))
    return found


def substitute_asset_handles(graph: Any, refs: dict[int, dict[str, Any]]) -> Any:
    """Return a copy of ``graph`` with each asset handle replaced by its
    ``core/ASSET`` reference dict, keyed by ``id(handle)``.
    """
    if _is_asset_handle(graph):
        return refs[id(graph)]
    if isinstance(graph, dict):
        return {k: substitute_asset_handles(v, refs) for k, v in graph.items()}
    if isinstance(graph, (list, tuple)):
        return [substitute_asset_handles(v, refs) for v in graph]
    return graph


# UI-export detection: the ComfyUI editor export carries these top-level keys,
# whereas the API format is a flat node-id -> node map. Catching it locally lets
# the SDK fail fast with a clear message instead of relying only on the server.
_UI_KEYS = ("nodes", "links", "last_node_id")


def looks_like_ui_format(graph: dict[str, Any]) -> bool:
    return isinstance(graph, dict) and all(k in graph for k in _UI_KEYS)
