"""
Agentic Orchestration (Code ↔ Memory correlation) tests.

Locks in the orchestration flow:
1. Read-time correlation — graph_explain/graph_query return related_memory.
2. Write-time feedback — a graph rebuild after an edit writes graphify_feedback obs.
3. context.md materialization — ctx_info mode="context" atomically writes context.md
   with Plan/Code/Memory sections; a later refresh replaces it.
4. context.md is readable through the allowed memory-bank file reader.

Uses the direct-tool-call pattern (no stdio).
"""

import json
from pathlib import Path

from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    if action == "graph_build":
        params = dict(params or {})
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _make_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / name
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text(f"proj_{name}", encoding="utf-8")
    return proj


# ── 1. read-time correlation ────────────────────────────────────────────────


async def test_graph_explain_returns_related_memory(tmp_path: Path):
    proj = _make_project(tmp_path, "rt")
    (proj / "a.py").write_text("def load_config():\n    return {}\n", encoding="utf-8")
    ws = str(proj)

    await action_call("graph_build", {"workspace_path": ws})
    await action_call(
        "mem_write",
        {
            "workspace_path": ws,
            "observations": [{"entityName": "load_config", "contents": ["pattern", "type: pattern"]}],
        },
    )

    r = await action_call("graph_explain", {"workspace_path": ws, "node": "load_config"})
    related = r["result"].get("related_memory", [])
    assert any(e["name"] == "load_config" and e["observations"] for e in related)

    r = await action_call("graph_query", {"workspace_path": ws, "query": "load_config"})
    assert any(e["name"] == "load_config" for e in r["result"].get("related_memory", []))


# ── 2. write-time feedback ──────────────────────────────────────────────────
