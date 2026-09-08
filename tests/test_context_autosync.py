"""context.md auto-sync guarantees (plan 5.x / task 7.1).

Locks in:
1. ``ctx_info mode="context"`` materializes ``.ai/memory-bank/context.md`` with the
   ``## Current Work & Handoff`` section (the docs' promised single source of truth).
2. A successful MUTATING action auto-refreshes context.md — no explicit ``ctx_info``
   call is needed after the mutation (dispatcher auto-sync hook).
3. ``ctx_info mode="snapshot"`` also refreshes context.md.
"""

import json

from mcp_server.modules import registration


def _tools():
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    params = dict(params or {})
    if action == "graph_build":
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _read_md(tmp_path) -> str | None:
    p = tmp_path / ".ai" / "memory-bank" / "context.md"
    return p.read_text(encoding="utf-8") if p.is_file() else None


async def test_context_md_has_handoff_section(tmp_path):
    await action_call("ctx_info", {"workspace_path": str(tmp_path), "mode": "context"})
    md = _read_md(tmp_path)
    assert md is not None
    assert "## Current Work & Handoff" in md


async def test_mutating_action_auto_refreshes_context_md(tmp_path):
    ws = str(tmp_path)
    await action_call("ctx_info", {"workspace_path": ws, "mode": "context"})
    before = _read_md(tmp_path)

    # A mutation (plan_create) must refresh context.md WITHOUT an explicit ctx_info.
    r = await action_call("plan_create", {"workspace_path": ws, "summary": "autosync plan"})
    assert r["success"] is True

    after = _read_md(tmp_path)
    assert after is not None
    assert after != before
    assert "autosync plan" in after  # the newly active plan is reflected


async def test_snapshot_refreshes_context_md(tmp_path):
    ws = str(tmp_path)
    await action_call("ctx_info", {"workspace_path": ws, "mode": "context"})
    first = _read_md(tmp_path)

    # Change state via a mutation, then a plain snapshot read must refresh the file.
    await action_call("plan_create", {"workspace_path": ws, "summary": "snapshot plan"})
    await action_call("ctx_info", {"workspace_path": ws, "mode": "snapshot"})

    md = _read_md(tmp_path)
    assert md is not None
    assert md != first
    assert "snapshot plan" in md
