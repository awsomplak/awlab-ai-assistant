"""Session counter + ctx_info mode=compact + mem_search metadata (plan f1155871).

Locks in:
1. ``ctx_info`` responses carry ``session.tool_calls_this_session`` (per-worker counter).
2. ``ctx_info mode="compact"`` returns a minimal post-compaction recovery snapshot.
3. ``mem_search`` returns ``total_matches`` / ``truncated`` metadata.
"""

import json

from mcp_server.helpers.session_state import session_call_count
from mcp_server.modules import registration


def _tools():
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    params = dict(params or {})
    return json.loads(await tool.fn(action=action, params=params))


async def test_ctx_info_carries_session_counter(tmp_path):
    ws = str(tmp_path)
    before = session_call_count()
    r1 = await action_call("ctx_info", {"workspace_path": ws, "mode": "snapshot"})
    assert r1["success"] is True
    assert r1["result"]["session"]["tool_calls_this_session"] == before + 1
    r2 = await action_call("ctx_info", {"workspace_path": ws, "mode": "snapshot"})
    assert r2["result"]["session"]["tool_calls_this_session"] == before + 2


async def test_ctx_info_compact_mode(tmp_path):
    ws = str(tmp_path)
    r = await action_call("ctx_info", {"workspace_path": ws, "mode": "compact"})
    assert r["success"] is True
    result = r["result"]
    assert result["mode"] == "compact"
    assert "session" in result
    assert "tool_calls_this_session" in result["session"]
    assert "plan" in result
    assert "family" in result
    assert "project_id" in result
    # Compact must NOT include the heavy composite fields.
    assert "code" not in result
    assert "memory" not in result


async def test_mem_search_returns_total_and_truncated(tmp_path):
    ws = str(tmp_path)
    await action_call(
        "mem_write",
        {
            "workspace_path": ws,
            "entities": [
                {"name": "Alpha", "entityType": "concept", "observations": ["alpha ledger"]},
                {"name": "Beta", "entityType": "concept", "observations": ["beta ledger"]},
                {"name": "Gamma", "entityType": "concept", "observations": ["gamma ledger"]},
            ],
        },
    )
    r = await action_call("mem_search", {"workspace_path": ws, "query": "ledger", "limit": 2})
    assert r["success"] is True
    result = r["result"]
    assert "total_matches" in result
    assert "truncated" in result
    assert result["truncated"] is True  # 3 matches, limit 2
    assert len(result["data"]) == 2
