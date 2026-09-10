"""
Phase 9 — Verify graph_* Guarantees.

Locks in:

1. Single-request flow — a graph read with no prior build auto-builds; after a
   source edit the next read auto-updates (not just no-op); the trace reports
   the executed steps.
2. Per-project isolation — two projects get separate .ai/codegraph/ dirs and
   never cross-contaminate (queries in project A never see project B symbols).

Uses the same direct-tool-call pattern as test_dispatcher_surface.py.
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


# ── Task 1: single-request flow ─────────────────────────────────────────────


async def test_single_request_auto_builds_then_auto_updates(tmp_path: Path):
    """Read with no build → auto-build (executed); edit a file → next read auto-updates."""
    sample = tmp_path / "sample.py"
    sample.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)

    # 1. No prior build → read auto-builds.
    r1 = await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    assert r1["success"] is True
    assert "graph_fresh" in r1["executed"]
    assert r1["result"]["count"] >= 1
    assert (tmp_path / ".ai" / "codegraph" / "graph.json").is_file()

    # 2. Fresh read (no change) → nothing re-runs.
    r2 = await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    assert r2["executed"] == []
    assert "graph_fresh" in r2["skipped"]

    # 3. Edit a source file → next read auto-updates (executed again).
    sample.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
    r3 = await action_call("graph_query", {"workspace_path": ws, "query": "bar"})
    assert r3["success"] is True
    assert "graph_fresh" in r3["executed"]  # stale → rebuild ran
    assert r3["result"]["count"] >= 1  # new symbol now searchable


async def test_single_request_edit_surfaces_new_symbol(tmp_path: Path):
    """After an edit, a previously-unknown symbol becomes queryable."""
    sample = tmp_path / "sample.py"
    sample.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)

    await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    before = await action_call("graph_query", {"workspace_path": ws, "query": "brand_new_sym"})
    assert before["result"]["count"] == 0

    sample.write_text("def brand_new_sym():\n    return 42\n", encoding="utf-8")
    after = await action_call("graph_query", {"workspace_path": ws, "query": "brand_new_sym"})
    assert after["success"] is True
    assert after["result"]["count"] >= 1


# ── Task 2: per-project isolation ───────────────────────────────────────────


async def test_per_project_isolation(tmp_path: Path):
    """Two projects get separate .ai/codegraph/ and never cross-contaminate."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / "alpha.py").write_text("def alpha_func():\n    return 'a'\n", encoding="utf-8")
    (proj_b / "beta.py").write_text("def beta_func():\n    return 'b'\n", encoding="utf-8")

    wa, wb = str(proj_a), str(proj_b)

    ra = await action_call("graph_query", {"workspace_path": wa, "query": "alpha_func"})
    rb = await action_call("graph_query", {"workspace_path": wb, "query": "beta_func"})
    assert ra["success"] is True and rb["success"] is True
    assert ra["result"]["count"] >= 1
    assert rb["result"]["count"] >= 1

    # A must NOT see B's symbol, and vice-versa.
    ra_beta = await action_call("graph_query", {"workspace_path": wa, "query": "beta_func"})
    rb_alpha = await action_call("graph_query", {"workspace_path": wb, "query": "alpha_func"})
    print("ra_beta:", ra_beta)
    print("rb_alpha:", rb_alpha)
    assert ra_beta["result"]["count"] == 0
    assert rb_alpha["result"]["count"] == 0

    # Separate codegraph dirs exist.
    assert (proj_a / ".ai" / "codegraph" / "graph.json").is_file()
    assert (proj_b / ".ai" / "codegraph" / "graph.json").is_file()


async def test_graph_status_reports_existence_per_project(tmp_path: Path):
    """graph_status reflects each project independently (exists/fresh)."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / "alpha.py").write_text("def alpha_func():\n    return 1\n", encoding="utf-8")

    # Only project A has been built.
    await action_call("graph_query", {"workspace_path": str(proj_a), "query": "alpha_func"})

    sa = await action_call("graph_status", {"workspace_path": str(proj_a)})
    sb = await action_call("graph_status", {"workspace_path": str(proj_b)})
    assert sa["result"].get("exists") is True
    assert sb["result"].get("exists") is False
