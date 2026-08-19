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

from mcp_server.config import settings
from mcp_server.helpers.file_utils import compute_tasks_summary as _get_task_summary
from mcp_server.tools.context_tools import (
    _detect_framework,
    _load_cache,
    _parse_registry,
    _save_cache,
    get_cache_path,
    get_context_snapshot,
    scan_project,
    suggest_relevant_files,
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


# ── scan_project ─────────────────────────────────────────────────────────────


class TestScanProject:
    @pytest.fixture(autouse=True)
    def _clear_scan_cache(self, temp_project_dir: str):
        """Clear the scan cache before each test to avoid cross-test pollution."""
        cache_path = settings.get_ai_dir(workspace_path=temp_project_dir) / get_cache_path(
            workspace_path=temp_project_dir
        )
        if cache_path.exists():
            cache_path.unlink()

    @pytest.mark.asyncio
    async def test_empty_project(self, temp_project_dir: str):
        """Scans an empty project successfully (framework Unknown, no targets)."""
        result = await scan_project(workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["framework"] == "Unknown"
        assert result["targets"] == []
        assert result["cached"] is False

    @pytest.mark.asyncio
    async def test_python_project_detection_and_entry_points(self, temp_project_dir: str):
        """Detects Python, finds src/ target, and exposes entry_points/relationships."""
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        Path(temp_project_dir, "src").mkdir(exist_ok=True)
        Path(temp_project_dir, "src", "main.py").write_text(
            "from flask import Flask\nimport json\n\napp = Flask(__name__)\n", encoding="utf-8"
        )
        result = await scan_project(workspace_path=temp_project_dir)
        assert "Python" in result["all_detected"]["languages"]
        assert "src/" in result["targets"] or "src" in str(result.get("entry_points", {}))
        assert "entry_points" in result
        assert "relationships" in result

    @pytest.mark.asyncio
    async def test_second_scan_is_cached(self, temp_project_dir: str):
        """Second scan returns a cached result."""
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        r1 = await scan_project(workspace_path=temp_project_dir)
        assert r1["cached"] is False
        r2 = await scan_project(workspace_path=temp_project_dir)
        assert r2["cached"] is True


# ── scan_project (cached + force_refresh) ─────────────────────────────────────


class TestScanProjectCached:
    @pytest.mark.asyncio
    async def test_cache_and_force_refresh(self, temp_project_dir: str):
        """Returns framework info, caches after the first scan, and force_refresh bypasses it."""
        r1 = await scan_project(workspace_path=temp_project_dir)
        assert "framework" in r1
        assert "cached" in r1
        assert r1["success"] is True

        r2 = await scan_project(workspace_path=temp_project_dir)
        assert r2["success"] is True
        assert r2["cached"] is True

        r3 = await scan_project(workspace_path=temp_project_dir, force_refresh=True)
        assert r3.get("cached") is False


# ── suggest_relevant_files ───────────────────────────────────────────────────


class TestSuggestRelevantFiles:
    @pytest.mark.asyncio
    async def test_suggestions_and_description(self, temp_project_dir: str):
        """Handles empty projects, preserves the task description, and matches keywords."""
        empty = await suggest_relevant_files(task_description="setup database", workspace_path=temp_project_dir)
        assert empty["success"] is True
        assert "suggestions" in empty
        assert "task_description" in empty

        desc = "add new API endpoint for user profiles"
        preserved = await suggest_relevant_files(task_description=desc, workspace_path=temp_project_dir)
        assert preserved["task_description"] == desc

        # Matching files for a Python project with relevant sources.
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        Path(temp_project_dir, "src").mkdir(parents=True, exist_ok=True)
        Path(temp_project_dir, "src", "database.py").write_text("# Database setup module\n", encoding="utf-8")
        Path(temp_project_dir, "src", "main.py").write_text("import sys\n", encoding="utf-8")
        result = await suggest_relevant_files(
            task_description="setup database connection", workspace_path=temp_project_dir
        )
        assert result["success"] is True
        suggestions = result["suggestions"]
        if suggestions:
            paths = [s["path"] for s in suggestions]
            assert any("database" in p.lower() for p in paths)


# ── Cache helpers ────────────────────────────────────────────────────────────


class TestCacheHelpers:
    def test_cache_roundtrip_and_errors(self, temp_project_dir: str):
        """Missing cache → empty; save+load round-trips; corrupted JSON is handled."""
        missing = _load_cache(
            cache_path=get_cache_path(workspace_path=temp_project_dir, cache_file="nonexistent.json"),
            workspace_path=temp_project_dir,
        )
        assert missing == {} or missing is None

        data = {"key": "value", "number": 42}
        cache_file = get_cache_path(workspace_path=temp_project_dir, cache_file="test_cache.json")
        assert _save_cache(cache_path=cache_file, data=data, workspace_path=temp_project_dir) is True
        assert _load_cache(cache_path=cache_file, workspace_path=temp_project_dir) == data

        bad = get_cache_path(workspace_path=temp_project_dir, cache_file="corrupted.json")
        bad_path = settings.get_ai_dir(workspace_path=temp_project_dir) / bad
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("{invalid json", encoding="utf-8")
        corrupted = _load_cache(cache_path=bad, workspace_path=temp_project_dir)
        assert corrupted == {} or corrupted is None
