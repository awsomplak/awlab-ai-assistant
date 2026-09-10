"""
Tests for tools/context_tools.py

Covers:
- get_context_snapshot: active plan, no plan, patterns
- search_memory_cross: search with results, empty results
- scan_project: framework detection, entry points, relationships
- get_project_fingerprint: cached vs fresh scan
- suggest_relevant_files: file suggestions based on task description
"""

from pathlib import Path

import pytest

from mcp_server.helpers.file_utils import compute_tasks_summary as _get_task_summary
from mcp_server.tools.context_tools import (
    _detect_framework,
    _parse_registry,
    get_context_snapshot,
)

# ── _get_task_summary ────────────────────────────────────────────────────────


class TestGetTaskSummary:
    def test_summarizes_status_counts(self):
        """Counts tasks by status (incl. subtasks); empty / no-task content → zeros."""
        content = """# Tasks

## Phase 1: Backend
- [x] Task 1: Setup DB
- [ ] Task 2: Create API
    - [ ] Task 2.1: Route
- [x✓] Task 3: Tested
- [⏳] Task 4: Deferred
- [!] Task 5: Failed
"""
        result = _get_task_summary(content)
        assert result["total"] == 6  # includes subtask Task 2.1
        assert result["completed"] == 2  # [x] + [x✓]
        assert result["pending"] == 2  # Task 2 + Task 2.1
        assert result["deferred"] == 1
        assert result["failed"] == 1
        assert result["skipped"] == 0

        empty = _get_task_summary("")
        assert empty["total"] == 0
        assert empty["completed"] == 0
        assert empty["pending"] == 0

        no_tasks = _get_task_summary("# Just a header\n\nSome text.")
        assert no_tasks["total"] == 0


# ── _parse_registry ──────────────────────────────────────────────────────────


class TestParseRegistry:
    def test_parses_all_sections(self, temp_project_dir: str, setup_registry_md: str):
        """Parses active, paused, and completed sections from the registry."""
        result = _parse_registry(temp_project_dir)
        assert len(result["active"]) > 0
        assert result["active"][0]["uuid"] == "a1b2c3d4"
        assert result["active"][0]["summary"] == "User authentication flow"
        assert len(result["paused"]) == 1
        assert result["paused"][0]["uuid"] == "e5f6g7h8"
        assert len(result["completed"]) == 1
        assert result["completed"][0]["uuid"] == "i9j0k1l2"

    def test_missing_registry_returns_empty(self, temp_project_dir: str):
        """Returns empty structure when the registry doesn't exist."""
        result = _parse_registry(temp_project_dir)
        assert result["active"] == []
        assert result["paused"] == []
        assert result["completed"] == []


# ── _detect_framework ────────────────────────────────────────────────────────


class TestDetectFramework:
    def test_detects_known_frameworks(self, temp_project_dir: str):
        """Detects Python, Node.js, and GitHub Actions by their markers."""
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        assert "Python" in _detect_framework(temp_project_dir)["languages"]

        Path(temp_project_dir, "package.json").write_text('{"name": "test"}', encoding="utf-8")
        assert "Node.js/JavaScript/TypeScript" in _detect_framework(temp_project_dir)["languages"]

        workflows = Path(temp_project_dir, ".github", "workflows")
        workflows.mkdir(parents=True, exist_ok=True)
        Path(workflows, "ci.yml").write_text("name: CI\n", encoding="utf-8")
        assert "GitHub Actions" in _detect_framework(temp_project_dir)["cicd"]

    def test_unknown_project(self, temp_project_dir: str):
        """Returns Unknown for an empty directory."""
        assert _detect_framework(temp_project_dir)["framework"] == "Unknown"


# ── get_context_snapshot ─────────────────────────────────────────────────────


class TestGetContextSnapshot:
    @pytest.mark.asyncio
    async def test_with_active_plan_includes_patterns(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str,
    ):
        """Returns active plan details, tasks summary, patterns, and project_id."""
        result = await get_context_snapshot(workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["active_plan"] is not None
        assert result["active_plan"]["uuid"] == plan_uuid
        assert result["active_plan"]["summary"] == "User authentication flow"
        assert result["active_plan"]["plan_details"] is not None
        assert "tasks_summary" in result["active_plan"]["plan_details"]
        assert "patterns" in result
        assert "project_id" in result

    @pytest.mark.asyncio
    async def test_no_active_plan(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
    ):
        """Returns no active plan when the registry is missing."""
        result = await get_context_snapshot(workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["active_plan"] is None
        assert result["patterns"] == []
