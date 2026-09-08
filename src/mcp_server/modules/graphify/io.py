from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from networkx.readwrite import json_graph as _jg

from .config import _codegraph_dir, _resolve_root


def _load_nx_graph(graph_path: Path):
    """Load a graph.json as a networkx Graph (graphify node-link format)."""

    data = json.loads(graph_path.read_text(encoding="utf-8"))
    if "links" not in data and "edges" in data:
        data = dict(data, links=data["edges"])
    try:
        return _jg.node_link_graph(data, edges="links")
    except TypeError:
        return _jg.node_link_graph(data)

def _graph_from_json(out_dir: Path) -> dict[str, Any] | None:
    """Load the prior graph.json as ``{nodes, edges}`` (or None if absent).

    graphify's JSON export stores edges under the ``links`` key (not ``edges``)
    — both are normalized here to the internal ``edges`` name.
    """
    path = out_dir / "graph.json"
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return {"nodes": d.get("nodes", []), "edges": d.get("links", d.get("edges", []))}
    except (OSError, json.JSONDecodeError):
        return None

def _load_graph(workspace_path: str | Path, root: str | Path | None = None) -> dict[str, Any] | None:
    """Load the built graph.json, or None if it doesn't exist.

    An ABSOLUTE ``root`` (e.g. the family graph dir from ``_resolve_graph_root``)
    is honored as the output location; a relative ``root`` (scan scope like
    ``"src"``) or None resolves to the WORKSPACE root's ``.ai/codegraph`` — so
    ``root="src"`` can never read/drop a graph inside ``src/``.
    """
    if root is not None and Path(root).is_absolute():
        out_dir = _codegraph_dir(Path(root))
    else:
        out_dir = _codegraph_dir(_resolve_root(workspace_path))
    graph_path = out_dir / "graph.json"
    if not graph_path.is_file():
        return None
    try:
        return json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

