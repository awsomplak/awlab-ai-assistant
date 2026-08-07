"""
Structured plan artifacts + memory-discovery orchestration tests.

Locks in:
1. Block-style task metadata (→ depends / ? if / → DONE notes) is parsed.
2. The tasks generator round-trips depends/if/notes + N-level nesting.
3. Dependencies resolve by dotted path (1.2), not just description substring.
4. plan.md / notes.md parse into structured fields.
5. ctx_info mode="context" surfaces plan_doc + notes_doc.
6. Empty-query context returns a memory inventory instead of blank (fresh-agent
   discovery — the "memory always empty" fix).

Uses direct function calls (no stdio).
"""

from pathlib import Path

from mcp_server.helpers.file_utils import (
    get_next_eligible_task,
    get_tasks_in_phase,
    parse_notes_md,
    parse_plan_md,
    parse_tasks_md,
)
from mcp_server.registry import _context_composite, _memory_inventory
from mcp_server.tools.utils_tools import format_tasks_as_markdown

BLOCK_TASKS = """# Tasks

## Phase 1: Test
- [ ] Task 1: First task
- [ ] Task 2: Second task
    → depends: 1.1
    ? if: user_ok
    → DONE: partly explored
    - [ ] Subtask 2.1
        → depends: 1.1
- [ ] Task 3: Third task
    → depends: Task 1
"""


# ── 1. Block-style metadata parsing ─────────────────────────────────────────


def test_parse_block_style_metadata():
    parsed = parse_tasks_md(BLOCK_TASKS)
    t2 = parsed["phases"][0]["tasks"][1]
    assert t2["path"] == "1.2"
    assert t2["depends"] == ["1.1"]
    assert t2["if"] == "user_ok"
    assert t2["notes"] == ["partly explored"]
    # Nested subtask keeps its own metadata.
    assert t2["subtasks"][0]["path"] == "1.2.1"
    assert t2["subtasks"][0]["depends"] == ["1.1"]
    # Legacy description-fragment dep is still captured.
    assert parsed["phases"][0]["tasks"][2]["depends"] == ["Task 1"]


def test_inline_depends_still_parsed_and_stripped():
    content = "# Tasks\n\n## Phase 1: X\n- [ ] Task 2: Do thing → depends: Task 1\n"
    parsed = parse_tasks_md(content)
    t = parsed["phases"][0]["tasks"][0]
    assert t["depends"] == ["Task 1"]
    assert "→ depends" not in t["description"]


def test_get_tasks_in_phase_includes_block_deps():
    tasks = get_tasks_in_phase(BLOCK_TASKS, 1)
    assert tasks is not None
    assert tasks[1]["dependencies"] == ["1.1"]
    assert tasks[1]["path"] == "1.2"


# ── 2. Generator round-trip ─────────────────────────────────────────────────


async def test_generator_round_trips_block_metadata():
    parsed = parse_tasks_md(BLOCK_TASKS)
    r = await format_tasks_as_markdown(phases=parsed["phases"])
    md = r["markdown"]
    assert "→ depends: 1.1" in md
    assert "? if: user_ok" in md
    assert "→ DONE: partly explored" in md
    # N-level nesting preserved.
    assert "        → depends: 1.1" in md

    re_parsed = parse_tasks_md(md)
    t2 = re_parsed["phases"][0]["tasks"][1]
    assert t2["depends"] == ["1.1"]
    assert t2["if"] == "user_ok"
    assert t2["notes"] == ["partly explored"]
    assert t2["subtasks"][0]["depends"] == ["1.1"]


# ── 3. Path-based dependency resolution ─────────────────────────────────────


def test_next_eligible_task_path_dep_blocks():
    content = """# Tasks

## Phase 1: Test
- [ ] Task 1: First task
- [ ] Task 2: Second task
    → depends: 1.1
- [ ] Task 3: Third task
"""
    # Task 1 eligible; Task 2 blocked by 1.1; Task 3 has no deps but comes after.
    n = get_next_eligible_task(content, 1)
    assert n is not None
    assert n["next_task"]["path"] == "1.1"

    # Complete Task 1 → Task 2 (dep 1.1 now met) becomes eligible before Task 3.
    done = content.replace("- [ ] Task 1: First task", "- [x] Task 1: First task")
    n2 = get_next_eligible_task(done, 1)
    assert n2 is not None
    assert n2["next_task"]["path"] == "1.2"


# ── 4. plan.md / notes.md structured parsing ────────────────────────────────


def test_parse_plan_md():
    plan = """# Plan: Demo Plan

## Overview
Build a demo.

## Approach
- Step 1
- Step 2

## Expected Outcomes
- Works
"""
    p = parse_plan_md(plan)
    assert p["name"] == "Plan: Demo Plan"
    assert "Build a demo" in p["overview"]
    assert p["approach"] == ["Step 1", "Step 2"]
    assert p["expected_outcomes"] == ["Works"]


def test_parse_notes_md():
    notes = """# Notes — Demo

## Key Decisions
- Use pnpm
- Keep it simple

## Constraints
- No secrets in .ai

## Risks
- Migrations risky
"""
    n = parse_notes_md(notes)
    assert n["name"] == "Notes — Demo"
    assert n["key_decisions"] == ["Use pnpm", "Keep it simple"]
    assert n["constraints"] == ["No secrets in .ai"]
    assert n["risks"] == ["Migrations risky"]


# ── 5. plan_doc + notes_doc surfaced in context composite ───────────────────


async def test_context_composite_surfaces_plan_and_notes(tmp_path: Path):
    proj = tmp_path / "p"
    (proj / ".ai" / "artifacts" / "aaaa1111").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "artifacts" / "registry.md").write_text(
        "# Plan Registry\n\n# Active Registry Plan\n\n| UUID | Status | Date | Summary |\n"
        "|------|--------|------|---------|\n| aaaa1111 | ⏹️ | 2026-08-07 10:00 | Demo |\n",
        encoding="utf-8",
    )
    (proj / ".ai" / "artifacts" / "aaaa1111" / "plan.md").write_text(
        "# Plan: Demo\n\n## Approach\n- Step A\n", encoding="utf-8"
    )
    (proj / ".ai" / "artifacts" / "aaaa1111" / "tasks.md").write_text(
        "# Tasks\n\n## Phase 1: X\n- [ ] Task 1: First\n", encoding="utf-8"
    )
    (proj / ".ai" / "artifacts" / "aaaa1111" / "notes.md").write_text(
        "# Notes — Demo\n\n## Key Decisions\n- Use pnpm\n", encoding="utf-8"
    )
    (proj / ".ai" / "project-id").write_text("proj_p", encoding="utf-8")

    r = await _context_composite(str(proj), query="")
    assert r["success"] is True
    assert r["plan_doc"]["name"] == "Plan: Demo"
    assert r["notes_doc"]["name"] == "Notes — Demo"
    assert r["notes_doc"]["key_decisions"] == ["Use pnpm"]
    # context.md includes the new sections.
    cm_path = Path(r["context_md"]["path"])
    content = cm_path.read_text(encoding="utf-8")
    assert "Plan Document (plan.md)" in content
    assert "Notes (notes.md)" in content


# ── 6. Memory discovery (empty query → inventory, not blank) ────────────────


async def test_context_composite_empty_query_returns_inventory(tmp_path: Path):
    proj = tmp_path / "m"
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text("proj_m", encoding="utf-8")
    ws = str(proj)

    # No memory yet → inventory reports 0 entities (not an error, not blank).
    r = await _context_composite(ws, query="")
    assert r["memory"]["mode"] == "inventory"
    assert r["memory"]["success"] is True
    assert "total_entities" in r["memory"]

    # After writing memory, the inventory lists it.
    from mcp_server.helpers.agent_recall import create_entities

    create_entities(
        workspace_path=ws,
        project_id="proj_m",
        entities=[{"name": "registry", "entityType": "concept", "observations": ["x"]}],
    )
    r2 = await _context_composite(ws, query="")
    assert r2["memory"]["mode"] == "inventory"
    assert r2["memory"]["total_entities"] >= 1
    names = [e["name"] for e in r2["memory"]["entities"]]
    assert "registry" in names


async def test_memory_inventory_helper(tmp_path: Path):
    proj = tmp_path / "i"
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text("proj_i", encoding="utf-8")
    ws = str(proj)

    from mcp_server.helpers.agent_recall import create_entities

    create_entities(
        workspace_path=ws,
        project_id="proj_i",
        entities=[
            {"name": "a", "entityType": "concept", "observations": ["1", "2"]},
            {"name": "b", "entityType": "pattern", "observations": ["3"]},
        ],
    )
    inv = await _memory_inventory(ws, project_id="proj_i")
    assert inv["success"] is True
    assert inv["mode"] == "inventory"
    assert inv["total_entities"] == 2
    assert inv["by_type"].get("concept") == 1
    assert inv["by_type"].get("pattern") == 1


# ── 7. mem_search entity_type filter (deterministic pattern listing) ────────


async def test_mem_search_entity_type_no_false_positive(tmp_path: Path):
    """entity_type='pattern' must NOT return entities whose observations merely
    mention 'type: pattern' (the old FTS hack returned false positives)."""
    proj = tmp_path / "pt"
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text("proj_pt", encoding="utf-8")
    ws = str(proj)

    from mcp_server.helpers.agent_recall import create_entities
    from mcp_server.tools.memory_tools import search_memory

    create_entities(
        workspace_path=ws,
        project_id="proj_pt",
        entities=[
            {
                "name": "pattern_use_pnpm",
                "entityType": "pattern",
                "observations": ["type: preference", "value: use pnpm"],
            },
            {"name": "SomeConcept", "entityType": "concept", "observations": ["mentions type: pattern in docs"]},
        ],
    )

    # Deterministic type listing (no query).
    r = await search_memory(workspace_path=ws, project_id="proj_pt", entity_type="pattern")
    assert r["success"] is True
    names = [e["name"] for e in r.get("data", [])]
    assert "pattern_use_pnpm" in names
    assert "SomeConcept" not in names  # the false positive must not appear

    # Type filter combined with a text query.
    r2 = await search_memory(workspace_path=ws, project_id="proj_pt", query="pnpm", entity_type="pattern")
    assert all(e["entityType"] == "pattern" for e in r2.get("data", []))


async def test_query_agent_recall_for_patterns_is_deterministic(tmp_path: Path):
    """query_agent_recall_for_patterns returns only entityType == 'pattern'."""
    proj = tmp_path / "pa"
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text("proj_pa", encoding="utf-8")
    ws = str(proj)

    from mcp_server.helpers.agent_recall import create_entities
    from mcp_server.tools.context_tools._memory_search import query_agent_recall_for_patterns

    create_entities(
        workspace_path=ws,
        project_id="proj_pa",
        entities=[
            {"name": "pattern_pref", "entityType": "pattern", "observations": ["value: use pnpm"]},
            {"name": "concept_mentions", "entityType": "concept", "observations": ["mentions pattern text"]},
        ],
    )
    pats = query_agent_recall_for_patterns(workspace_path=ws, project_id="proj_pa", limit=10)
    names = [p["name"] for p in pats]
    assert "pattern_pref" in names
    assert "concept_mentions" not in names
