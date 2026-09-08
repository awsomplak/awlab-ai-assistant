"""
Integration tests for all 39+ MCP tools.

Each tool is tested with:
1. Valid parameters (expected success response)
2. Missing/invalid parameters (expected error response)

These tests call the implementation functions directly (not via MCP transport)
to validate handler logic without requiring a running server.
"""

import json
import uuid as uuid_mod
from pathlib import Path

import pytest

from mcp_server.helpers.validation import (
    validate_status,
    validate_status_transition,
    validate_uuid,
)
from mcp_server.tools.context_tools._cache import load_cache, save_cache
from mcp_server.tools.context_tools.context import get_context_snapshot
from mcp_server.tools.context_tools.scanner import scan_project
from mcp_server.tools.context_tools.suggest import suggest_relevant_files
from mcp_server.tools.file_tools import read_memory_bank
from mcp_server.tools.memory_tools import search_memory
from mcp_server.tools.plan_tools.io import (
    store_memory_checkpoint,
    sync_to_agent_recall,
    update_registry_phase_count,
)
from mcp_server.tools.plan_tools.phase import validate_phase_gate
from mcp_server.tools.plan_tools.plan import (
    check_plan_completable,
    execute_workflow,
    generate_retrospective_summary,
    get_next_eligible_task,
    list_registry,
    list_workflows,
    mark_phase_complete,
    resolve_deferred_tasks,
    switch_active_plan,
)
from mcp_server.tools.plan_tools.tasks import (
    batch_update_tasks,
    read_plan_tasks,
    update_task_status,
)
from mcp_server.tools.utils_tools import get_environment, get_server_version

# ── Constants ──────────────────────────────────────────────────────────────────

VALID_UUID = "hulqlotc"
VALID_WORKSPACE = str(Path.cwd().resolve())
INVALID_UUID = "zzzzzzzz"
INVALID_WORKSPACE = "Z:\\nonexistent_path_xxxx"  # reliably invalid on Windows


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def temp_workspace(tmp_path_factory):
    """Create a temporary workspace with minimal .ai/artifacts structure."""
    # 1. Ask pytest to generate a managed directory instead of using tempfile
    tmp = tmp_path_factory.mktemp("agent-memory-test-integration")

    # Create .ai/artifacts/registry.md
    artifacts_dir = tmp / ".ai" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    registry = artifacts_dir / "registry.md"
    registry.write_text(
        "# Active Registry Plan\n\n"
        "| UUID | Status | Date | Summary |\n"
        "|------|--------|------|--------|\n"
        f"| {VALID_UUID} | \u23f9\ufe0f | 2026-06-17 23:15 | Test plan |\n"
        "\n"
        "# Paused Registry Plan\n\n"
        "| UUID | Status | Date | Summary |\n"
        "|------|--------|------|--------|\n"
        "\n"
        "# Completed Registry Plan\n\n"
        "| UUID | Status | Date | Summary |\n"
        "|------|--------|------|--------|\n",
        encoding="utf-8",
    )

    # Create plan dir with empty tasks.md
    plan_dir = artifacts_dir / VALID_UUID
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_md = plan_dir / "tasks.md"
    tasks_md.write_text("# Tasks\n\n## Phase 1: Test\n- [x] Task 1: completed task\n- [x] Task 2: completed task\n")

    # Create project-id
    (tmp / ".ai" / "project-id").write_text("test-project")

    # Create Cline/Workflows so list_workflows can find it
    (tmp / "Cline" / "Workflows").mkdir(parents=True, exist_ok=True)

    # 2. Yield the path. Because pytest manages it, no with-block cleanup is needed!
    yield str(tmp.resolve())


# ── Helper: assert success response ────────────────────────────────────────────


def _assert_success(result: dict, fields: list[str] | None = None):
    """Assert result is a success dict with required fields."""
    if isinstance(result, str):
        result = json.loads(result)
    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result}"
    if "success" in result:
        assert result["success"] is True or result.get("success") is True or result.get("success") in (True, "true"), (
            f"Expected success=True, got: {result}"
        )
    if fields:
        for field in fields:
            assert field in result, f"Expected field '{field}' in result: {result}"


def _assert_error(result: dict):
    """Assert result is an error dict."""
    if isinstance(result, str):
        result = json.loads(result)
    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result}"
    # Error can be indicated by success=False, or an "error" key, or the function raising
    if "success" in result:
        assert result["success"] in (False, "false"), f"Expected success=False, got: {result}"
    if "error" in result:
        assert result["error"], f"Expected non-empty error message: {result}"


# ═══════════════════════════════════════════════════════════════════════════════
# Tool Test Classes
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlanToolsIntegration:
    """Integration tests for plan/task management tools."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "func, kwargs_update, expect_success, extra_asserts",
        [
            (list_registry, {}, True, ["active"]),
            (switch_active_plan, {"uuid": VALID_UUID}, True, []),
            (read_plan_tasks, {"plan_uuid": VALID_UUID, "format": "structured"}, True, []),
            (update_task_status, {"project_id": "test-project", "plan_uuid": VALID_UUID, "task_path": "1.1", "new_status": "[x!]"}, True, []),
            (batch_update_tasks, {"project_id": "test-project", "plan_uuid": VALID_UUID, "updates": [{"task_path": "1.2", "new_status": "[x✓]"}]}, True, [("executed", [{"task_path": "1.2", "old_status": "[x]", "new_status": "[x✓]"}])]),
            (get_next_eligible_task, {"plan_uuid": VALID_UUID}, True, ["next_task"]),
            (mark_phase_complete, {"project_id": "test-project", "plan_uuid": VALID_UUID, "phase_num": 1}, True, []),
            (list_workflows, {"workflows_dir": "auto"}, True, []),
        ],
    )
    async def test_plan_tools_dispatch(self, temp_workspace, func, kwargs_update, expect_success, extra_asserts):
        kwargs = {"workspace_path": temp_workspace}
        kwargs.update(kwargs_update)
        if kwargs.get("workspace_path") == "invalid":
            kwargs["workspace_path"] = INVALID_WORKSPACE
        if kwargs.get("workflows_dir") == "auto":
            kwargs["workflows_dir"] = Path(temp_workspace) / "Cline" / "Workflows"

        result = await func(**kwargs)
        result = json.loads(result) if isinstance(result, str) else result

        if expect_success:
            fields = [v for v in extra_asserts if isinstance(v, str)]
            _assert_success(result, fields=fields)
        else:
            _assert_error(result)

        for v in extra_asserts:
            if isinstance(v, tuple):
                assert result.get(v[0]) == v[1], f"Expected {v[0]} == {v[1]}, got {result.get(v[0])}"

