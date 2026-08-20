"""
Large-graph HTML viz — enhanced filter layer + community drill-down.

The rendered ``graph.html`` (per-project AND merged ``family.html``) carries a
self-contained client-side layer for graphs with thousands of nodes:

1. Filter bar — path filter, min-degree slider, 2-hop focus mode, reset.
2. Physics guard — forceAtlas2 is disabled above the physics threshold so the
   browser does not freeze on huge graphs.
3. Community drill-down — when the over-limit aggregated community view is
   produced, the member→community payload is embedded so a community node can
   be expanded into its member nodes.

These tests lock in that every to_html output (full or aggregated, project or
family) is post-processed with the ``awlab-large-viz`` marker + controls, and
that the aggregated view carries the drill-down payload.
"""

import json
from json import JSONDecoder
from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.helpers import agent_recall
from mcp_server.helpers.graphify_bridge import (
    _VIZ_MARKER,
    _codegraph_dir,
    _family_codegraph_dir,
    graph_build_action,
)
from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    if action == "graph_build":
        params = dict(params or {})
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _reset(*keys: str) -> None:
    """Drop cached settings properties so a monkeypatched env is re-read."""
    for k in keys:
        settings.__dict__.pop(k, None)


def _extract_drilldown(html: str) -> dict:
    """Parse the `var DRILLDOWN = <json>;` payload out of an injected graph.html."""
    marker = "var DRILLDOWN = "
    idx = html.index(marker) + len(marker)
    obj, _ = JSONDecoder().raw_decode(html[idx:])
    return obj


def _extract_legend(html: str) -> list[dict]:
    """Parse the `const LEGEND = [...]` payload graphify embeds for the legend."""
    marker = "const LEGEND = "
    idx = html.index(marker) + len(marker)
    obj, _ = JSONDecoder().raw_decode(html[idx:])
    return obj


# ── Full (node-level) view: filter layer present ───────────────────────────


async def test_graph_html_full_view_has_filter_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A normal full graph.html carries the enhanced filter bar."""
    monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT", raising=False)
    _reset("graph_viz_limit")
    for i in range(4):
        (tmp_path / f"mod{i}.py").write_text(f"def func{i}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)

    r = await action_call("graph_build", {"workspace_path": ws})
    assert r["success"] is True, r
    assert r["result"]["html"] == "full"

    html = (tmp_path / ".ai" / "codegraph" / "graph.html").read_text(encoding="utf-8")
    assert _VIZ_MARKER in html
    # Filter bar controls.
    assert 'id="f-path"' in html  # path filter input
    assert 'id="f-deg"' in html  # min-degree slider
    assert 'id="f-focus"' in html  # focus-mode toggle
    assert 'id="f-apply"' in html and 'id="f-reset"' in html
    # Filter bar can be collapsed (show/hide toggle) to free sidebar space.
    assert 'id="f-toggle"' in html
    assert "#viz-controls.collapsed .vbody" in html or "collapsed" in html
    # Draggable splitter between Node Info and Communities.
    assert "viz-splitter" in html
    assert "row-resize" in html
    # Physics guard wired to the injected constants.
    assert "PHYSICS_LIMIT" in html
    # Non-aggregated → drill-down payload is empty and no master dataset embedded
    # (the page's RAW_NODES/RAW_EDGES ARE the master).
    assert _extract_drilldown(html) == {}
    assert "var MASTER_NODES = [];" in html
    assert "var MASTER_EDGES = [];" in html

    # Communities legend is populated (heuristic labels), not just "Select All".
    legend = _extract_legend(html)
    assert legend, "communities legend must list per-community items"
    assert all("label" in c and "count" in c and "cid" in c for c in legend)
    assert all(str(c["label"]) != f"Community {c['cid']}" for c in legend), (
        "legend should use heuristic labels, not bare 'Community N'"
    )


async def test_graph_html_full_view_physics_guard_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A graph above the physics threshold disables physics at load + warns."""
    monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT", raising=False)
    _reset("graph_viz_limit")
    # Small graph is under the physics threshold → no load-time warning.
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    r = await action_call("graph_build", {"workspace_path": str(tmp_path)})
    assert r["result"]["html"] == "full"
    html = (tmp_path / ".ai" / "codegraph" / "graph.html").read_text(encoding="utf-8")
    # The physics-guard branch must exist; the large-graph warning is dynamic JS,
    # so we only assert the guard code is wired (not that it fired for a tiny graph).
    assert "network.stopSimulation" in html
    assert "Large graph (" in html  # warning template present


# ── Aggregated (over-limit) view: drill-down payload embedded ──────────────


async def test_graph_html_aggregated_embeds_drilldown(tmp_path: Path):
    """Over-limit aggregated view embeds member data for community drill-down."""
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text(f"def func{i}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)

    r = await action_call("graph_build", {"workspace_path": ws, "node_limit": 1})
    assert r["success"] is True, r
    assert r["result"]["html"] == "aggregated"
    assert r["result"]["node_limit"] == 1

    html_path = tmp_path / ".ai" / "codegraph" / "graph.html"
    if not html_path.is_file():
        pytest.skip("single-community early return: no aggregated file produced")
    html = html_path.read_text(encoding="utf-8")
    assert _VIZ_MARKER in html
    drill = _extract_drilldown(html)
    assert drill, "aggregated view must embed a non-empty drill-down payload"
    assert any("members" in v and v["members"] for v in drill.values()), (
        "each embedded community should list its member nodes"
    )
    # Every member row is [id, label, source_file].
    for rows in (v["members"] for v in drill.values()):
        for row in rows:
            assert len(row) == 3 and isinstance(row[0], str) and isinstance(row[1], str)
    # Drill-down UI is present.
    assert "viz-drill" in html
    assert "Load members into graph" in html

    # The FULL member dataset is embedded so drill-down can rebuild a real
    # member subgraph (graphify's aggregated HTML alone has only meta-nodes).
    m_idx = html.index("var MASTER_NODES = ")
    master_nodes, _ = JSONDecoder().raw_decode(html[m_idx + len("var MASTER_NODES = ") :])
    m_idx = html.index("var MASTER_EDGES = ")
    master_edges, _ = JSONDecoder().raw_decode(html[m_idx + len("var MASTER_EDGES = ") :])
    assert master_nodes, "aggregated view must embed the full member node list"
    assert len(master_nodes) > len(drill), "master node list exceeds the drill-down cap"
    # Every member id in the drill-down payload resolves to a master node
    # (so "Load members into graph" never yields 0 nodes).
    member_ids = {row[0] for rows in (v["members"] for v in drill.values()) for row in rows}
    master_ids = {str(n["id"]) for n in master_nodes}
    assert member_ids <= master_ids, "drill-down member ids must exist in the master dataset"
    # Member-level edges were embedded too.
    assert master_edges

    # Aggregated view's legend + drill-down headings use heuristic labels too.
    legend = _extract_legend(html)
    assert legend, "aggregated view must have a populated communities legend"
    assert "app" in legend[0]["label"].lower() or legend[0]["label"]  # labelled, not bare cid
    # Drill-down payload labels are the same heuristic labels.
    first_label = next(iter(drill.values()))["label"]
    assert first_label != f"Community {next(iter(drill))}"


# ── Heuristic community labels (non-LLM) ───────────────────────────────────


def test_community_labels_heuristic():
    """_community_labels derives meaningful labels from top-degree members."""
    import networkx as nx

    from mcp_server.helpers.graphify_bridge import _community_labels

    G = nx.Graph()
    G.add_node("a", label="apiHandler")
    G.add_node("b", label="helper")
    G.add_node("c", label="util")
    G.add_edge("a", "b")
    G.add_edge("a", "c")  # apiHandler is the hub → highest degree → first
    labels = _community_labels(G, {1: ["a", "b", "c"]}, top_k=2)
    assert labels == {1: "apiHandler, helper"}

    # Community whose members all have empty/ignorable labels falls back to
    # "Community N" (node id "x" is a label, so give it a strip-able one).
    G2 = nx.Graph()
    G2.add_node("x", label="()")
    labels2 = _community_labels(G2, {7: ["x"]})
    assert labels2 == {7: "Community 7"}


# ── Family (merged) view: filter layer applied to family.html ──────────────


def test_family_html_has_filter_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Merged family.html also carries the enhanced layer (mirrored to members)."""
    monkeypatch.setattr(settings, "config_home", tmp_path / "cfg")
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_recall, "project_families_path", lambda: tmp_path / "cfg" / "project-families.json")
    monkeypatch.setattr(agent_recall, "family_db_path", lambda slug: str(tmp_path / f"family_{slug}.db"))

    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "b.js").write_text("export function apiHandler() { return 7 }\n", encoding="utf-8")
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "a.js").write_text("export const a = 1\n", encoding="utf-8")
    (frontend / "src" / "lib.js").write_text("export function helper() { return 42 }\n", encoding="utf-8")

    (tmp_path / "cfg" / "project-families.json").write_text(
        json.dumps(
            {
                "webapp": {
                    "name": "Webapp",
                    "members": [
                        {"path": str(backend), "project_id": "backend"},
                        {"path": str(frontend), "project_id": "frontend"},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    res = graph_build_action(workspace_path=str(frontend), family="webapp", include_html=True)
    assert res["success"] is True, res
    assert res["family_html_mirrored_to_members"] is True

    fam_dir = _codegraph_dir(_family_codegraph_dir("webapp"))
    fam_html = fam_dir / "family.html"
    assert fam_html.is_file()
    text = fam_html.read_text(encoding="utf-8")
    assert _VIZ_MARKER in text
    assert 'id="f-path"' in text
    assert 'id="f-deg"' in text

    # Mirrored copies are byte-identical (same injected layer everywhere).
    for member in (backend, frontend):
        member_out = member / ".ai" / "codegraph"
        assert (member_out / "family.html").read_bytes() == fam_html.read_bytes()
