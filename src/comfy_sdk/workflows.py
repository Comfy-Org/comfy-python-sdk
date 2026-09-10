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

    def add_node(
        self,
        class_type: str,
        *,
        before: str | None = None,
        after: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> str:
        """Insert a new node, redirecting downstream connections through it.

        The new node is assigned an auto-incremented ID (one greater than the
        highest existing node ID).  ``class_type`` and optional ``inputs`` are
        stored on the node.  Any link in ``inputs`` (e.g.
        ``{"model": ["2", 0]}``) that points to an existing node causes *all*
        downstream consumers of that source output to be redirected to the new
        node's corresponding output.

        ``before`` / ``after`` are informational — they document which existing
        node the new node is placed relative to, but do not affect the
        redirection logic (which is driven entirely by the links in ``inputs``).

        Args:
            class_type: ComfyUI class type (e.g. ``"KSampler"``).
            before: If set, the new node is inserted before this node.
            after: If set, the new node is inserted after this node.
            inputs: Input dict for the new node. Links in this dict drive
                downstream redirection.

        Returns:
            The auto-generated node ID.

        Raises:
            ValueError: If both ``before`` and ``after`` are given.
        """
        if before and after:
            raise ValueError("Specify either 'before' or 'after', not both")

        # Auto-generate node_id: one greater than the highest existing ID
        if self.json:
            max_id = max(int(nid) for nid in self.json)
            node_id = str(max_id + 1)
        else:
            node_id = "1"

        node_entry: dict[str, Any] = {"class_type": class_type}
        if inputs:
            node_entry["inputs"] = inputs
        self.json[node_id] = node_entry

        new_inputs = inputs or {}

        # Collect (upstream_node_id, output_index) pairs from links in inputs
        upstream_outputs: dict[str, set[int]] = {}
        for value in new_inputs.values():
            if _is_link(value):
                src_node = value[0]
                src_output = int(value[1])
                if src_node in self.json:
                    upstream_outputs.setdefault(src_node, set()).add(src_output)

        # Redirect downstream consumers of those upstream outputs
        for src_node, output_indices in upstream_outputs.items():
            for nid, node in self.json.items():
                if nid == node_id:
                    continue
                node_inputs = node.get("inputs")
                if not node_inputs:
                    continue
                for key, value in list(node_inputs.items()):
                    if _is_link(value) and value[0] == src_node and int(value[1]) in output_indices:
                        node_inputs[key] = [node_id, value[1]]

        return node_id

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
