"""Phase 8 — integration: mem_observe → bake → delivery → ctx_info pattern_candidates.

End-to-end flow through the real ``action_call`` dispatcher (no stdio, same pattern as
``test_orchestration.py``):

1. ``mem_observe`` records 4 recurring signal variants → the inline baking tick bakes
   1 candidate (``cmd_pnpm``).
2. ``ctx_info mode="context"`` surfaces ``pattern_candidates`` AND advances the delivery
   marker (tell-once).
3. A second ``ctx_info`` returns NO ``pattern_candidates`` (never re-told).
4. ``mem_search`` (store=patterns) returns the baked candidate in ``baked_patterns``
   (stack-scoped; candidates persist in baked.json after delivery).
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_server.modules import registration

OBSERVATIONS = [
    {"signature": "cmd_pnpm", "value": "always use pnpm install", "source": "explicit", "stack": "any"},
    {"signature": "cmd_pnpm", "value": "Always use pnpm install.", "source": "explicit", "stack": "any"},
    {"signature": "cmd_pnpm", "value": "always  use pnpm install!", "source": "explicit", "stack": "any"},
    {"signature": "cmd_pnpm", "value": "ALWAYS USE PNPM INSTALL", "source": "explicit", "stack": "any"},
]


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    return json.loads(await tool.fn(action=action, params=params))


def _make_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / name
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text(f"proj_{name}", encoding="utf-8")
    return proj


async def test_observe_bake_deliver_ctx_info(tmp_path: Path):
    proj = _make_project(tmp_path, "pb")
    ws = str(proj)

    # 1. Observe 4 distinct variants → the inline tick bakes 1 candidate.
    for obs in OBSERVATIONS:
        r = await action_call("mem_observe", {"workspace_path": ws, "observations": [obs]})
        assert r["result"]["appended"] == 1  # distinct raw value → distinct fingerprint

    # 2. ctx_info surfaces pattern_candidates (and marks them delivered).
    r = await action_call("ctx_info", {"workspace_path": ws, "mode": "context", "query": "pnpm"})
    cands = r["result"].get("pattern_candidates") or []
    assert len(cands) == 1
    assert cands[0]["signature"] == "cmd_pnpm"

    # 3. tell-once → the second ctx_info has nothing new to tell.
    r2 = await action_call("ctx_info", {"workspace_path": ws, "mode": "context", "query": "pnpm"})
    assert (r2["result"].get("pattern_candidates") or []) == []


async def test_baked_patterns_in_mem_search(tmp_path: Path):
    proj = _make_project(tmp_path, "pb2")
    ws = str(proj)
    for obs in OBSERVATIONS:
        await action_call("mem_observe", {"workspace_path": ws, "observations": [obs]})

    r = await action_call("mem_search", {"workspace_path": ws, "store": "patterns", "query": "pnpm"})
    baked = r["result"].get("baked_patterns") or []
    assert len(baked) == 1
    assert baked[0]["signature"] == "cmd_pnpm"
