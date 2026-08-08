"""
Phase 8 — Verify Consolidation Against Copilot Constraints.

Locks in the guarantees of the 2-tool dispatcher surface:

1. Tool count stays tiny (2 tools) + schema context budget is small.
2. action_help error loop — unknown action → did_you_mean; invalid params → invalid list.
3. Orchestration — preconditions auto-run (idempotent), trace (executed/skipped) present,
   no partial execution (a stale graph read auto-builds, then the next read is fresh).

These call the tools directly through the FastMCP tool registry (no stdio transport),
matching the pattern used by the rest of the test suite.
"""

import json
from pathlib import Path

from mcp_server.modules import registration
from mcp_server.registry import REGISTRY

# ── Helpers ──────────────────────────────────────────────────────────────────


def _tools() -> dict:
    """Return the FastMCP tool registry dict {name: Tool}."""
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    """Invoke the action_call tool directly, returning the parsed JSON payload."""
    tool = _tools()["action_call"]
    raw = await tool.fn(action=action, params=params)
    return json.loads(raw)


async def action_help(action: str | None = None) -> str:
    """Invoke the action_help tool directly."""
    tool = _tools()["action_help"]
    return await tool.fn(action=action)


# ── Task 1: tool count + schema context budget ──────────────────────────────


def test_only_two_tools_exposed():
    """Copilot caps ~15 tools/server — we expose exactly 2."""
    assert sorted(_tools()) == ["action_call", "action_help"]


def test_schema_context_budget_is_small():
    """Combined tool JSON schema must stay tiny (Copilot context budget)."""
    total = 0
    for name, tool in _tools().items():
        params = getattr(tool, "parameters", None)
        raw = json.dumps(params) if params is not None else "{}"
        total += len(raw)
        assert len(raw) < 2000, f"{name} schema too large ({len(raw)} chars)"
    assert total < 4000


def test_registry_has_expected_action_count():
    """20 actions across 7 groups — matches the documented surface.

    16 baseline + 2 memory-auditing actions (``mem_list_entities`` +
    ``mem_dedupe``) + 1 offline-cache replay action (``mem_replay``) +
    ``reg_update`` (single registry.md CRUD: create / update / delete).
    """
    assert len(REGISTRY) == 20
    groups = {spec["group"] for spec in REGISTRY.values()}
    assert {"task", "plan", "memory", "graph", "context", "util", "workflow"} <= groups


# ── Task 2: error loop — unknown action / invalid params / help ─────────────


async def test_unknown_action_returns_did_you_mean():
    r = await action_call("tasks")
    assert r["success"] is False
    assert "did_you_mean" in r
    assert r["did_you_mean"]  # non-empty suggestions
    assert "valid_actions" in r
    assert "help" in r


async def test_invalid_params_reports_each_problem():
    r = await action_call("task_read", {"plan_uuid": ""})
    assert r["success"] is False
    assert "invalid" in r
    reasons = {item["param"] for item in r["invalid"]}
    assert "workspace_path" in reasons  # required but missing
    assert "plan_uuid" in reasons  # pattern mismatch


async def test_action_help_per_action_usage():
    h = await action_help("graph_build")
    assert "graph_build" in h


async def test_action_help_overview_groups_all_actions():
    h = await action_help(None)
    # Overview must enumerate every registered action.
    for name in REGISTRY:
        assert f"`{name}`" in h


# ── Task 3: orchestration — precondition auto-run, trace, idempotency ───────


async def test_graph_read_auto_builds_and_traces(tmp_path: Path):
    """First read on a fresh project auto-builds the graph (executed trace); no partial exec."""
    (tmp_path / "sample.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    r = await action_call("graph_query", {"workspace_path": str(tmp_path), "query": "foo"})
    assert r["success"] is True
    assert "graph_fresh" in r["executed"]  # precondition did work (built)
    assert "workspace_valid" in r["skipped"]  # pure gate skipped
    assert r["result"]["count"] >= 1


async def test_graph_read_second_call_is_idempotent(tmp_path: Path):
    """Once fresh, the next read skips the build — no repeated work."""
    (tmp_path / "sample.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)

    first = await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    assert "graph_fresh" in first["executed"]

    second = await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    assert second["success"] is True
    assert second["executed"] == []  # nothing re-ran
    assert "graph_fresh" in second["skipped"]  # now fresh → skipped
