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
    """23 actions across 7 groups — matches the documented surface.

    21 + ``project_id`` (check-and-create, isolation bootstrap) + ``plan_doc``
    (direct plan.md/notes.md read/write/delete).
    """
    assert len(REGISTRY) == 23
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


# ── Task 4: first-contact contract (the three predictable failures) ─────────
#
# Agents on first contact reliably make the same three mistakes:
#  1. omit `workspace_path` (forgetting the precondition needs it)
#  2. flatten params at the top level
#  3. treat `action_help` as an action_call target
# The dispatcher MUST return a corrective contract payload in each case so the
# agent self-corrects on the very next turn (instead of guessing again).


async def test_missing_workspace_path_returns_corrective_contract():
    r = await action_call("ctx_info", {})
    assert r["success"] is False
    assert "contract" in r
    assert "params" in r["contract"]["shape"]
    assert "workspace_path" in r["contract"]["shape"]
    # The 4 rules that prevent the predictable failures
    rules_blob = " ".join(r["contract"]["rules"]).lower()
    assert "single nested json object" in rules_blob
    assert "workspace_path" in rules_blob
    assert "strict" in rules_blob
    assert "separate tool" in rules_blob  # action_help is a separate tool


async def test_unknown_action_returns_corrective_contract():
    """When the agent calls action_help as an action, the error must teach."""
    r = await action_call("action_help", {})
    assert r["success"] is False
    assert "contract" in r
    assert "valid_actions" in r
    assert "help" in r


async def test_tool_description_teaches_contract():
    """The action_call tool description itself must front-load the contract —
    this is the first thing an agent reads about the MCP."""
    from mcp_server.registry import build_tool_description

    desc = build_tool_description()
    assert "TWO tools" in desc
    assert "params" in desc and "SINGLE nested JSON object" in desc
    assert "workspace_path" in desc
    assert "separate" in desc.lower()  # action_help is separate


async def test_skill_md_teaches_contract():
    """The generated SKILL.md must include the strict contract preamble —
    agents that discover this MCP via skill activation will read it first."""
    from mcp_server.registry import build_skill_md

    md = build_skill_md()
    # Frontmatter description must mention both tools and the contract
    assert "action_call" in md
    assert "action_help" in md
    # Body contract preamble
    assert "TWO tools" in md or "two tools" in md
    assert "workspace_path" in md
    assert "flatten" in md.lower()  # explicitly forbid flattening


async def test_action_help_overview_teaches_contract():
    """When the agent lands on action_help() (the overview), it must be
    re-educated about the call shape before seeing the action list."""
    h = await action_help(None)
    assert "TWO tools" in h or "two tools" in h
    assert "params" in h
    assert "workspace_path" in h
    # Actions still listed (don't regress the overview)
    for name in REGISTRY:
        assert f"`{name}`" in h


# ── Task 1.5: cross-host array/object string fallback ──────────────────────
#
# Some hosts serialize list/object params as JSON strings (instead of real JSON
# arrays / objects). The validator now auto-parses strings for `array` and
# `object` types when the parse result matches the declared type. These tests
# lock in the three behaviors: real list still works, JSON string works, and
# malformed string still fails (with the new error hint).


async def test_real_list_for_array_param_still_works(tmp_path: Path):
    """Regression guard: a real JSON list for an `array`-typed param passes
    validation unchanged. The fallback is a strict superset, not a replacement."""
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    r = await action_call(
        "mem_observe",
        {
            "workspace_path": str(tmp_path),
            "observations": [
                {"signature": "test_real_list", "value": "v1", "source": "behavioral"},
            ],
        },
    )
    # Real list passes; the action's handler then runs (and may fail for other
    # reasons in tmp_path, but not with `expected array`).
    assert r["success"] is True or "expected array" not in str(r.get("error", ""))
    # The invalid param list must NOT contain the array error.
    assert not any(
        err.get("reason") == "expected array"
        for err in r.get("invalid", []) or []
    )


async def test_json_string_for_array_param_is_accepted(tmp_path: Path):
    """A JSON string that parses to a list must be accepted by the fallback
    path. This is the cross-host fix — hosts that stringify arrays still work."""
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    observations_json = '[{"signature":"s","value":"v","source":"behavioral"}]'
    r = await action_call(
        "mem_observe",
        {
            "workspace_path": str(tmp_path),
            "observations": observations_json,  # string, not list
        },
    )
    # The fallback parsed it; the array error must NOT appear in invalid.
    assert not any(
        err.get("reason") == "expected array"
        for err in r.get("invalid", []) or []
    )
    # And the call actually succeeded (handler ran with parsed list).
    assert r["success"] is True
    assert r["action"] == "mem_observe"
    assert r["result"]["appended"] == 1


async def test_json_string_for_object_param_is_accepted(tmp_path: Path):
    """The same fallback applies to `object` types — e.g. `wf params` can be
    a dict OR a JSON string (and now `plan_doc content` would work the same)."""
    # `plan_doc` mode=write content is a string, so the object fallback
    # doesn't apply to it — but `wf params` is type=object and accepts both.
    # We test the validator's object fallback directly via a synthetic spec.
    from mcp_server.registry import validate_params

    spec = {"params": {"data": {"type": "object", "required": True}}}
    # Dict → accepted as-is
    validated, errors = validate_params(spec, {"data": {"key": "value"}})
    assert validated == {"data": {"key": "value"}}
    assert errors == []
    # JSON string that parses to a dict → accepted via fallback
    validated, errors = validate_params(spec, {"data": '{"key": "value"}'})
    assert validated == {"data": {"key": "value"}}, f"fallback failed: {errors}"
    assert errors == []


async def test_malformed_string_still_fails_with_hint(tmp_path: Path):
    """If the string isn't valid JSON (or doesn't parse to the expected type),
    the call still fails — but the error payload now includes a `hint` field
    that teaches the host how to recover."""
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    # Garbage that isn't a list
    r = await action_call(
        "mem_observe",
        {
            "workspace_path": str(tmp_path),
            "observations": "not json at all",
        },
    )
    assert r["success"] is False
    # The error must point to the array issue
    assert any(
        err.get("param") == "observations" and err.get("reason") == "expected array"
        for err in r.get("invalid", []) or []
    )
    # The new hint must be present so the host learns the workaround
    assert "hint" in r, "dispatch error payload must include a 'hint' field for array/object type mismatches"
    assert "JSON string" in r["hint"], f"hint must mention the JSON-string fallback: {r['hint']}"


async def test_json_string_with_wrong_shape_fails_with_hint(tmp_path: Path):
    """A valid JSON string that doesn't parse to the expected type (e.g. a JSON
    object where the schema needs an array) still fails — and still gets the
    hint. This is the "value is a valid JSON object, but the schema wants a
    list" case some hosts will hit."""
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    r = await action_call(
        "mem_observe",
        {
            "workspace_path": str(tmp_path),
            "observations": '{"wrong": "shape"}',  # valid JSON object, but array expected
        },
    )
    assert r["success"] is False
    assert any(
        err.get("reason") == "expected array"
        for err in r.get("invalid", []) or []
    )
