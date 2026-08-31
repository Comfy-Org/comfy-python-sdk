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


def extra_data_for(api_key: str | None) -> dict[str, Any] | None:
    """Build the wire ``extra_data`` object carrying the partner API key.

    ``api_key`` authenticates partner (API) nodes (e.g. Gemini) embedded in a
    workflow — it is unrelated to the client's own ``Authorization`` bearer
    token. Returns ``None`` when no key is supplied, so callers omit
    ``extra_data`` from the request entirely rather than sending an empty
    object.
    """
    if not api_key:
        return None
    return {"api_key_comfy_org": api_key}


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

def validate_idempotency_key(key: str) -> str:
    """``key`` unchanged, or ``ValueError`` for one the contract cannot carry.

    The vendored router contract pins ``Idempotency-Key`` to 1-255 characters,
    and the value rides an HTTP header, so a control character (CR/LF above
    all) or a non-ASCII byte never produces a keyed request — it produces a
    transport-layer error after the round trip, or worse, a smuggled header.
    Refusing locally, before any bytes move, mirrors what ``parse_model_id``
    does for path segments and closes the emptiness trap specifically: the
    call sites mint a fresh key for ``None``, and without this check an
    explicit ``""`` fell into that same branch — so a caller who meant "collect
    under my key" but passed an empty one silently dispatched a SECOND billed
    generation instead.
    """
    if not key:
        raise ValueError(
            "idempotency_key must not be empty; pass None to have one minted"
        )
    if len(key) > 255:
        raise ValueError(
            f"idempotency_key is {len(key)} characters; the contract caps it at 255"
        )
    if not key.isascii() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise ValueError(
            "idempotency_key must be printable ASCII: it is sent as an HTTP "
            "header, and a control or non-ASCII character fails at the "
            "transport instead of being keyed"
        )
    return key
