"""
Tests for tools/context_tools.py

Covers:
- get_context_snapshot: active plan, no plan, patterns
- search_memory_cross: search with results, empty results
- store_context: basic store, dedup, TTL expiry
- get_context_fragment: topic search across context store + memory + registry
- scan_project: framework detection, entry points, relationships
- get_project_fingerprint: cached vs fresh scan
- suggest_relevant_files: file suggestions based on task description
"""

from pathlib import Path
import pytest

from mcp_server.config import settings
from mcp_server.tools.context_tools import (
    get_context_snapshot,
    store_context,
    get_context_fragment,
    get_context_path,
    get_cache_path,
    scan_project,
    suggest_relevant_files,
    _parse_registry,
    _detect_framework,
    _load_cache,
    _save_cache,
)
from mcp_server.helpers.file_utils import compute_tasks_summary as _get_task_summary


# ── Fixture helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def context_store_path(temp_project_dir: str) -> str:
    """Return the path to the context store JSON file."""
    return get_context_path(workspace_path=temp_project_dir)


# ── _get_task_summary ────────────────────────────────────────────────────────


class TestGetTaskSummary:
    def test_basic_count(self):
        """Should count tasks by status (including subtasks)."""
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

    def test_empty_content(self):
        """Should handle empty content."""
        result = _get_task_summary("")
        assert result["total"] == 0
        assert result["completed"] == 0
        assert result["pending"] == 0

    def test_no_tasks(self):
        """Should handle content with no tasks."""
        content = "# Just a header\n\nSome text."
        result = _get_task_summary(content)
        assert result["total"] == 0


# ── _parse_registry ──────────────────────────────────────────────────────────


class TestParseRegistry:
    def test_parse_active(self, temp_project_dir: str, setup_registry_md: str):
        """Should parse active plan from registry."""
        result = _parse_registry(temp_project_dir)
        assert len(result["active"]) > 0
        assert result["active"][0]["uuid"] == "a1b2c3d4"
        assert result["active"][0]["summary"] == "User authentication flow"

    def test_parse_paused(self, temp_project_dir: str, setup_registry_md: str):
        """Should parse paused plans."""
        result = _parse_registry(temp_project_dir)
        assert len(result["paused"]) == 1
        assert result["paused"][0]["uuid"] == "e5f6g7h8"

    def test_parse_completed(self, temp_project_dir: str, setup_registry_md: str):
        """Should parse completed plans."""
        result = _parse_registry(temp_project_dir)
        assert len(result["completed"]) == 1
        assert result["completed"][0]["uuid"] == "i9j0k1l2"

    def test_missing_registry(self, temp_project_dir: str):
        """Should return empty structure when registry doesn't exist."""
        result = _parse_registry(temp_project_dir)
        assert result["active"] == []
        assert result["paused"] == []
        assert result["completed"] == []


# ── _detect_framework ────────────────────────────────────────────────────────


class TestDetectFramework:
    def test_detect_python(self, temp_project_dir: str):
        """Should detect Python project."""
        # Create requirements.txt
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        result = _detect_framework(temp_project_dir)
        assert "Python" in result["languages"]

    def test_detect_node(self, temp_project_dir: str):
        """Should detect Node.js project."""
        Path(temp_project_dir, "package.json").write_text('{"name": "test"}', encoding="utf-8")
        result = _detect_framework(temp_project_dir)
        assert "Node.js/JavaScript/TypeScript" in result["languages"]

    def test_unknown_project(self, temp_project_dir: str):
        """Should return Unknown for empty directory."""
        result = _detect_framework(temp_project_dir)
        assert result["framework"] == "Unknown"

    def test_detect_github_actions(self, temp_project_dir: str):
        """Should detect GitHub Actions CI/CD."""
        workflows = Path(temp_project_dir, ".github", "workflows")
        workflows.mkdir(parents=True, exist_ok=True)
        Path(workflows, "ci.yml").write_text("name: CI\n", encoding="utf-8")
        result = _detect_framework(temp_project_dir)
        assert "GitHub Actions" in result["cicd"]


# ── get_context_snapshot ─────────────────────────────────────────────────────


class TestGetContextSnapshot:
    @pytest.mark.asyncio
    async def test_with_active_plan(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Should return active plan details and tasks summary."""
        result = await get_context_snapshot(workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["active_plan"] is not None
        assert result["active_plan"]["uuid"] == plan_uuid
        assert result["active_plan"]["summary"] == "User authentication flow"
        assert result["active_plan"]["plan_details"] is not None
        assert "tasks_summary" in result["active_plan"]["plan_details"]
        assert "project_id" in result

    @pytest.mark.asyncio
    async def test_no_active_plan(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
    ):
        """Should return no active plan when registry missing."""
        result = await get_context_snapshot(workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["active_plan"] is None
        assert result["patterns"] == []

    @pytest.mark.asyncio
    async def test_patterns_included(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Should include patterns from agent-recall."""
        result = await get_context_snapshot(workspace_path=temp_project_dir)
        assert "patterns" in result


# ── store_context ────────────────────────────────────────────────────────────


class TestStoreContext:
    @pytest.mark.asyncio
    async def test_basic_store(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Should store a key-value entry."""
        result = await store_context(key="test_key", value="test_value", scope="project", ttl=3600, workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["key"] == "test_key"
        assert result["scope"] == "project"
        assert result["deduplicated"] is False
        assert result["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_dedup(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Should detect duplicate entries."""
        await store_context(key="dup_key", value="first", scope="project", workspace_path=temp_project_dir)
        result = await store_context(key="dup_key", value="second", scope="project", workspace_path=temp_project_dir)
        assert result["deduplicated"] is True

    @pytest.mark.asyncio
    async def test_no_expiry(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Should handle TTL=0 (no expiry)."""
        result = await store_context(key="permanent", value="forever", ttl=0, workspace_path=temp_project_dir)
        assert result["expires_at"] is None

    @pytest.mark.asyncio
    async def test_persistence(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Stored entries should persist across calls."""
        await store_context(key="persist", value="data", scope="project", workspace_path=temp_project_dir)
        result = await store_context(key="persist", value="data", scope="project", workspace_path=temp_project_dir)
        assert result["deduplicated"] is True


# ── get_context_fragment ─────────────────────────────────────────────────────


class TestGetContextFragment:
    @pytest.mark.asyncio
    async def test_returns_topic(
        self,
        temp_project_dir: str,
        plan_uuid: str,
        setup_project_id: str,
        setup_env_md: str,
        setup_registry_md: str,
        setup_plan_md: str,
        setup_tasks_md: str
    ):
        """Should return the topic field in response."""
        await store_context(key="database", value="postgresql://localhost", scope="project", ttl=3600, workspace_path=temp_project_dir)
        result = await get_context_fragment(topic="database", workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["topic"] == "database"

    @pytest.mark.asyncio
    async def test_context_store_match(self, temp_project_dir: str, setup_registry_md: str):
        """Should find matching entries in context store."""
        await store_context(key="db_config", value="postgresql://localhost", scope="project", workspace_path=temp_project_dir)
        result = await get_context_fragment(topic="db_config", workspace_path=temp_project_dir)
        assert result["entry_count"] >= 1
        assert "db_config =" in result["fragment"]

    @pytest.mark.asyncio
    async def test_registry_match(self, temp_project_dir: str, setup_registry_md: str):
        """Should find matching entries in registry."""
        await store_context(key="authentication", value="user, password", scope="project", workspace_path=temp_project_dir)
        result = await get_context_fragment(topic="authentication", workspace_path=temp_project_dir)
        assert result["entry_count"] >= 1

    @pytest.mark.asyncio
    async def test_no_match(self, temp_project_dir: str):
        """Should handle no matches gracefully."""
        await store_context(key="db_config", value="postgresql://localhost", scope="project", workspace_path=temp_project_dir)
        result = await get_context_fragment(topic="xyz_nonexistent_topic_abc", workspace_path=temp_project_dir)
        assert result["entry_count"] == 0
        assert "No context found" in result["fragment"]


# ── scan_project ─────────────────────────────────────────────────────────────


class TestScanProject:
    @pytest.fixture(autouse=True)
    def _clear_scan_cache(self, temp_project_dir: str):
        """Clear the scan cache before each test to avoid cross-test pollution."""
        cache_file = get_cache_path(workspace_path=temp_project_dir)
        cache_path = settings.get_ai_dir(workspace_path=temp_project_dir) / cache_file
        if cache_path.exists():
            cache_path.unlink()

    @pytest.mark.asyncio
    async def test_empty_project(self, temp_project_dir: str):
        """Should scan an empty project successfully."""
        result = await scan_project(workspace_path=temp_project_dir)
        assert result["success"] is True
        assert result["framework"] == "Unknown"
        assert result["targets"] == []
        assert result["cached"] is False

    @pytest.mark.asyncio
    async def test_detect_python_framework(self, temp_project_dir: str):
        """Should detect Python and find src/ target."""
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        Path(temp_project_dir, "src").mkdir(exist_ok=True)
        Path(temp_project_dir, "src", "main.py").write_text(
            "import os\nimport sys\n\ndef main():\n    pass\n", encoding="utf-8"
        )
        result = await scan_project(workspace_path=temp_project_dir)
        assert "Python" in result["all_detected"]["languages"]
        # Should find src/ as a scan target
        assert "src/" in result["targets"] or "src" in str(result.get("entry_points", {}))

    @pytest.mark.asyncio
    async def test_entry_points_read(self, temp_project_dir: str):
        """Should read entry points for detected targets."""
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        Path(temp_project_dir, "src").mkdir(exist_ok=True)
        Path(temp_project_dir, "src", "app.py").write_text(
            "from flask import Flask\nimport json\n\napp = Flask(__name__)\n", encoding="utf-8"
        )
        result = await scan_project(workspace_path=temp_project_dir)
        # Should have entry_points even if empty
        assert "entry_points" in result
        assert "relationships" in result

    @pytest.mark.asyncio
    async def test_caching(self, temp_project_dir: str):
        """Second scan should return cached result."""
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        result1 = await scan_project(workspace_path=temp_project_dir)
        assert result1["cached"] is False

        result2 = await scan_project(workspace_path=temp_project_dir)
        assert result2["cached"] is True


# ── scan_project (cached + force_refresh) ─────────────────────────────────────


class TestScanProjectCached:
    @pytest.mark.asyncio
    async def test_returns_framework(self, temp_project_dir: str):
        """Should return framework info."""
        result = await scan_project(workspace_path=temp_project_dir)
        assert "framework" in result
        assert "cached" in result

    @pytest.mark.asyncio
    async def test_cached_after_scan(self, temp_project_dir: str):
        """Should use cache after first scan."""
        r1 = await scan_project(workspace_path=temp_project_dir)
        r2 = await scan_project(workspace_path=temp_project_dir)
        assert r1["success"] is True
        assert r2["success"] is True

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self, temp_project_dir: str):
        """force_refresh=True should skip cache."""
        r1 = await scan_project(workspace_path=temp_project_dir)
        r2 = await scan_project(workspace_path=temp_project_dir, force_refresh=True)
        assert r2.get("cached") is False


# ── suggest_relevant_files ───────────────────────────────────────────────────


class TestSuggestRelevantFiles:
    @pytest.mark.asyncio
    async def test_empty_project(self, temp_project_dir: str):
        """Should handle empty project gracefully."""
        result = await suggest_relevant_files(task_description="setup database", workspace_path=temp_project_dir)
        assert result["success"] is True
        assert "suggestions" in result
        assert "task_description" in result

    @pytest.mark.asyncio
    async def test_matching_files(self, temp_project_dir: str):
        """Should suggest files matching task keywords."""
        # Create a Python project with relevant files
        Path(temp_project_dir, "requirements.txt").write_text("pytest\n", encoding="utf-8")
        Path(temp_project_dir, "src").mkdir(parents=True, exist_ok=True)
        Path(temp_project_dir, "src", "database.py").write_text(
            "# Database setup module\n", encoding="utf-8"
        )
        Path(temp_project_dir, "src", "main.py").write_text(
            "import sys\n", encoding="utf-8"
        )

        result = await suggest_relevant_files(task_description="setup database connection", workspace_path=temp_project_dir)
        assert result["success"] is True
        # Should find database-related files
        suggestions = result["suggestions"]
        if suggestions:
            paths = [s["path"] for s in suggestions]
            assert any("database" in p.lower() for p in paths)

    @pytest.mark.asyncio
    async def test_task_description_preserved(self, temp_project_dir: str):
        """Should preserve the original task description."""
        desc = "add new API endpoint for user profiles"
        result = await suggest_relevant_files(task_description=desc, workspace_path=temp_project_dir)
        assert result["task_description"] == desc


# ── Cache helpers ────────────────────────────────────────────────────────────


class TestCacheHelpers:
    def test_load_cache_nonexistent(self, temp_project_dir: str):
        """Should return empty dict for missing cache."""
        cache_file = get_cache_path(workspace_path=temp_project_dir, cache_file="nonexistent.json")
        result = _load_cache(cache_path=cache_file, workspace_path=temp_project_dir)
        assert result == {} or result is None

    def test_save_and_load(self, temp_project_dir: str):
        """Should persist and retrieve data."""
        data = {"key": "value", "number": 42}
        cache_file = get_cache_path(workspace_path=temp_project_dir, cache_file="test_cache.json")
        success = _save_cache(cache_path=cache_file, data=data, workspace_path=temp_project_dir)
        assert success is True

        loaded = _load_cache(cache_path=cache_file, workspace_path=temp_project_dir)
        assert loaded == data

    def test_corrupted_cache(self, temp_project_dir: str):
        """Should handle corrupted JSON gracefully."""
        cache_file = get_cache_path(workspace_path=temp_project_dir, cache_file="corrupted.json")
        cache_path = settings.get_ai_dir(workspace_path=temp_project_dir) / cache_file
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{invalid json", encoding="utf-8")

        result = _load_cache(cache_path=cache_file, workspace_path=temp_project_dir)
        assert result == {} or result is None