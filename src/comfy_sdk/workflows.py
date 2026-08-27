"""Workflow construction and mutation.

A :class:`Workflow` is a thin, local wrapper over the raw API-format graph. The
graph stays a freely-mutable ``dict`` (``wf.json``); ``set_input`` is sugar for
``wf.json[node][\"inputs\"][field] = value`` that also accepts an asset handle
(substituted into a ``core/ASSET`` object at submit time). Construction does no
network I/O in v1.
"""

from __future__ import annotations

import json as _json
from os import PathLike
from typing import Any


def _is_link(obj: Any) -> bool:
    """Return ``True`` if ``obj`` is a ComfyUI API-format connection link
    (``[node_id: str, output_index: int]``)."""
    if not isinstance(obj, list):
        return False
    if len(obj) != 2:
        return False
    if not isinstance(obj[0], str):
        return False
    if not isinstance(obj[1], int) and not isinstance(obj[1], float):
        return False
    return True


class Workflow:
    """An API-format ComfyUI graph, ready to submit.

    Build one via ``client.workflows`` (:meth:`WorkflowFactory.from_file` or
    :meth:`WorkflowFactory.from_json`) rather than constructing it directly.
    This is the API format exported by "Save (API Format)" — not the UI
    format, which the SDK rejects locally at submit with
    :class:`WorkflowFormatUi` before any request is made. The raw graph stays
    available and mutable as :attr:`json`.
    """

    def __init__(self, graph: dict[str, Any]) -> None:
        self.json = graph

    def set_input(self, node_id: str, field: str, value: Any) -> None:
        """Set ``node.inputs.field``. ``value`` may be a plain JSON value or an
        asset handle; handles are substituted into ``core/ASSET`` objects when
        the workflow is submitted.
        """
        node = self.json.setdefault(node_id, {})
        inputs = node.setdefault("inputs", {})
        inputs[field] = value

    def remove_node(self, node_id: str) -> None:
        """Remove a node and redirect links through it back to their sources.

        Deletes the node identified by ``node_id`` from the graph. Any input
        connections (links) in other nodes that reference this node's outputs
        are redirected to the source that fed into the removed node, effectively
        unwinding any insertion point.

        If the removed node has exactly one input that is a link, all downstream
        consumers of its outputs are redirected to that source. Otherwise (zero
        or multiple link inputs), downstream links are simply deleted.
        """
        removed = self.json.pop(node_id, None)
        if removed is None:
            return

        # Collect link inputs from the removed node
        link_inputs: list[tuple[str, int]] = []
        removed_inputs = removed.get("inputs") or {}
        for value in removed_inputs.values():
            if _is_link(value):
                link_inputs.append((value[0], int(value[1])))

        if len(link_inputs) == 1:
            # Single link input: redirect all downstream consumers to that source
            src_node, src_output = link_inputs[0]
            for node in self.json.values():
                inputs = node.get("inputs")
                if not inputs:
                    continue
                for key, value in list(inputs.items()):
                    if _is_link(value) and value[0] == node_id:
                        if src_node in self.json:
                            inputs[key] = [src_node, src_output]
                        else:
                            del inputs[key]
        else:
            # Zero or multiple link inputs: just delete downstream links
            for node in self.json.values():
                inputs = node.get("inputs")
                if not inputs:
                    continue
                to_delete = []
                for key, value in inputs.items():
                    if _is_link(value) and value[0] == node_id:
                        to_delete.append(key)
                for key in to_delete:
                    del inputs[key]

    def __repr__(self) -> str:
        return f"Workflow(nodes={len(self.json)})"


class WorkflowFactory:
    """``client.workflows`` — alternative constructors for :class:`Workflow`.

    Namespaced on the client (rather than free-standing) because construction is
    expected to become client-bound once server-side subgraphs land; in v1 it is
    purely local.
    """

    def from_file(self, path: str | PathLike[str]) -> Workflow:
        with open(path, encoding="utf-8") as fh:
            return Workflow(_json.load(fh))

    def from_json(self, graph: dict[str, Any]) -> Workflow:
        return Workflow(graph)

    def from_str(self, text: str) -> Workflow:
        return Workflow(_json.loads(text))
