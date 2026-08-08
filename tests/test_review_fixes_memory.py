"""
Regression tests for the live-test review MEMORY findings (plan 2l6iavva, Phases 3-4).

Locks in the fixes:
1. ``mem_remove`` type-safety — an ambiguous bare name is REFUSED with a candidate
   list (no silent data loss); ``entities=[{name, entityType}]`` deletes exactly
   the right entity.
2. No empty duplicates — observation/relation auto-create (and graphify feedback)
   REUSES an existing same-named entity instead of spawning a ``(name, concept)`` row.
3. ``mem_list_entities`` — deterministic inventory (name/entityType/obs count).
4. ``mem_dedupe`` — dry-run preview then apply: keeps the data-bearing entity,
   archives the empty duplicate.

Uses the direct-tool-call pattern with per-project isolation (via .ai/project-id).
"""

import json
from pathlib import Path

from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    return json.loads(await tool.fn(action=action, params=params))


def _make_project(tmp_path: Path, name: str) -> Path:
    """Isolated project (per-project DB file via .ai/project-id)."""
    proj = tmp_path / name
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text(f"proj_{name}", encoding="utf-8")
    return proj


# ── 1. mem_remove type-safety ───────────────────────────────────────────────


async def test_mem_remove_refuses_ambiguous_bare_name(tmp_path: Path):
    proj = _make_project(tmp_path, "mr1")
    ws = str(proj)
    await action_call(
        "mem_write",
        {
            "workspace_path": ws,
            "entities": [
                {"name": "Project Progress", "entityType": "feature", "observations": ["real data"]},
                {"name": "Project Progress", "entityType": "concept", "observations": []},
            ],
        },
    )

    # Bare name matches 2 entities → MUST refuse (never guess / destroy data).
    r = await action_call("mem_remove", {"workspace_path": ws, "names": ["Project Progress"]})
    assert r["result"]["success"] is True
    assert r["result"]["archived"]["deleted"] == 0
    assert any("Ambiguous name" in b for b in r["result"]["archived"]["blocked"])

    # Precise spec deletes ONLY the empty concept; the data-bearing feature survives.
    r2 = await action_call(
        "mem_remove", {"workspace_path": ws, "entities": [{"name": "Project Progress", "entityType": "concept"}]}
    )
    assert r2["result"]["archived"]["deleted"] == 1

    rd = await action_call("mem_read", {"workspace_path": ws, "node": "Project Progress"})
    nodes = rd["result"].get("nodes") or []
    assert len(nodes) == 1
    assert nodes[0]["entityType"] == "feature"
    assert any("real data" in o for o in nodes[0].get("observations", []))


# ── 2. no empty duplicates on auto-create ───────────────────────────────────


async def test_mem_write_reuses_existing_same_named_entity(tmp_path: Path):
    proj = _make_project(tmp_path, "nd1")
    ws = str(proj)
    await action_call(
        "mem_write",
        {"workspace_path": ws, "entities": [{"name": "Bus Service", "entityType": "feature", "observations": ["o1"]}]},
    )

    # Observation referencing the same name must NOT spawn an empty concept dupe.
    await action_call(
        "mem_write", {"workspace_path": ws, "observations": [{"entityName": "Bus Service", "contents": ["new obs"]}]}
    )

    r = await action_call("mem_list_entities", {"workspace_path": ws})
    bus = [e for e in r["result"]["entities"] if e["name"] == "Bus Service"]
    assert len(bus) == 1  # no duplicate
    assert bus[0]["entityType"] == "feature"
    assert bus[0]["observation_count"] == 2


async def test_rebuild_does_not_duplicate_graphify_feedback(tmp_path: Path):
    proj = _make_project(tmp_path, "fbdup")
    sample = proj / "a.py"
    sample.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ws = str(proj)

    await action_call("graph_build", {"workspace_path": ws})  # first build: no feedback
    sample.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
    await action_call("graph_build", {"workspace_path": ws})  # rebuild 1: feedback
    sample.write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return 2\n\ndef baz():\n    return 3\n", encoding="utf-8"
    )
    await action_call("graph_build", {"workspace_path": ws})  # rebuild 2: feedback again

    r = await action_call("mem_list_entities", {"workspace_path": ws})
    fb = [e for e in r["result"]["entities"] if e["name"] == "graphify_feedback"]
    assert len(fb) == 1  # reused, not duplicated
    assert fb[0]["observation_count"] >= 2  # both rebuilds appended observations


# ── 3. mem_list_entities inventory ──────────────────────────────────────────


async def test_mem_list_entities_inventory(tmp_path: Path):
    proj = _make_project(tmp_path, "ml1")
    ws = str(proj)
    await action_call(
        "mem_write",
        {
            "workspace_path": ws,
            "entities": [
                {"name": "Alpha", "entityType": "pattern", "observations": ["x"]},
                {"name": "Beta", "entityType": "concept", "observations": []},
            ],
        },
    )

    r = await action_call("mem_list_entities", {"workspace_path": ws})
    assert r["result"]["success"] is True
    assert r["result"]["total_entities"] >= 2
    assert isinstance(r["result"]["by_type"], dict)
    names = {e["name"] for e in r["result"]["entities"]}
    assert {"Alpha", "Beta"} <= names
    alpha = next(e for e in r["result"]["entities"] if e["name"] == "Alpha")
    assert alpha["entityType"] == "pattern"
    assert alpha["observation_count"] == 1


# ── 4. mem_dedupe: dry-run then apply ───────────────────────────────────────


async def test_mem_dedupe_dry_run_then_apply(tmp_path: Path):
    proj = _make_project(tmp_path, "dd1")
    ws = str(proj)
    await action_call(
        "mem_write",
        {
            "workspace_path": ws,
            "entities": [
                {"name": "PrimeVue 5", "entityType": "entity", "observations": ["a", "b", "c"]},
                {"name": "PrimeVue 5", "entityType": "concept", "observations": []},
            ],
        },
    )

    # Dry run: preview only, nothing mutated.
    dr = await action_call("mem_dedupe", {"workspace_path": ws, "name": "PrimeVue 5", "dry_run": True})
    assert dr["result"]["dry_run"] is True
    assert len(dr["result"]["groups"]) == 1
    assert dr["result"]["groups"][0]["keeper"]["entityType"] == "entity"
    assert dr["result"]["archived"] == 0

    # Apply: keep the data-bearing entity, archive the empty dupe.
    da = await action_call("mem_dedupe", {"workspace_path": ws, "name": "PrimeVue 5", "dry_run": False})
    assert da["result"]["dry_run"] is False
    assert da["result"]["archived"] == 1

    r = await action_call("mem_list_entities", {"workspace_path": ws})
    pv = [e for e in r["result"]["entities"] if e["name"] == "PrimeVue 5"]
    assert len(pv) == 1
    assert pv[0]["entityType"] == "entity"
    assert pv[0]["observation_count"] == 3
