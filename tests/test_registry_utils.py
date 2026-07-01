"""
Tests for helpers/registry_utils.py

Covers:
- parse_registry (three-table extraction)
- switch_active (changing active plan)
- Switch scenarios: plan already active, plan in paused, plan in completed
"""

from pathlib import Path
from mcp_server.helpers.registry_utils import parse_registry, switch_active_plan


class TestParseRegistry:
    def test_parse_full_registry(self, temp_project_dir: str, setup_registry_md: str):
        """Should parse all three tables correctly."""
        result = parse_registry(temp_project_dir)
        assert result["success"] is True
        assert len(result["active"]) == 1
        assert len(result["paused"]) == 1
        assert len(result["completed"]) == 1

        # Active
        active = result["active"][0]
        assert active["uuid"] == "a1b2c3d4"
        assert active["status"] == "⏹️"
        assert "authentication" in active["summary"].lower()

        # Paused
        paused = result["paused"][0]
        assert paused["uuid"] == "e5f6g7h8"
        assert paused["status"] == "⏸️"

        # Completed
        completed = result["completed"][0]
        assert completed["uuid"] == "i9j0k1l2"
        assert completed["status"] == "✅"

    def test_missing_registry(self, temp_project_dir: str):
        """Should return empty tables when no registry exists."""
        result = parse_registry(temp_project_dir)
        assert result["success"] is True
        assert result["active"] == []
        assert result["paused"] == []
        assert result["completed"] == []

    def test_empty_registry(self, temp_project_dir: str):
        """Should handle a registry with headers but no rows."""
        registry_path = Path(temp_project_dir) / ".ai" / "artifacts" / "registry.md"
        content = """# Active Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|

# Paused Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|

# Completed Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
"""
        registry_path.write_text(content, encoding="utf-8")
        result = parse_registry(temp_project_dir)
        assert result["success"] is True
        assert result["active"] == []
        assert result["paused"] == []
        assert result["completed"] == []


class TestSwitchActivePlan:
    def test_switch_from_active_to_paused(self, temp_project_dir: str, setup_registry_md: str):
        """When activating a paused plan, the old active should become paused."""
        result = switch_active_plan(temp_project_dir, "e5f6g7h8")
        assert result["success"] is True
        assert result["new_active_uuid"] == "e5f6g7h8"

        # Verify the registry was updated
        registry = parse_registry(temp_project_dir)
        assert len(registry["active"]) == 1
        assert registry["active"][0]["uuid"] == "e5f6g7h8"
        assert registry["active"][0]["status"] == "⏹️"

        # Old active (a1b2c3d4) should now be paused
        assert any(p["uuid"] == "a1b2c3d4" and p["status"] == "⏸️" for p in registry["paused"])

    def test_switch_from_active_to_completed(self, temp_project_dir: str, setup_registry_md: str):
        """When activating a completed plan, it should move to active."""
        result = switch_active_plan(temp_project_dir, "i9j0k1l2")
        assert result["success"] is True
        assert result["new_active_uuid"] == "i9j0k1l2"

        registry = parse_registry(temp_project_dir)
        assert registry["active"][0]["uuid"] == "i9j0k1l2"
        assert registry["active"][0]["status"] == "⏹️"

    def test_switch_already_active(self, temp_project_dir: str, setup_registry_md: str):
        """Switching to the already-active plan should succeed without changes."""
        result = switch_active_plan(temp_project_dir, "a1b2c3d4")
        assert result["success"] is True
        assert result["new_active_uuid"] == "a1b2c3d4"

        registry = parse_registry(temp_project_dir)
        registry_path = Path(temp_project_dir) / ".ai" / "artifacts" / "registry.md"
        original_content = registry_path.read_text(encoding="utf-8")

        # Re-read and confirm it's the same
        registry2 = parse_registry(temp_project_dir)
        assert registry2["active"][0]["uuid"] == "a1b2c3d4"

    def test_switch_nonexistent_uuid(self, temp_project_dir: str, setup_registry_md: str):
        """Switching to a UUID not in any table should return error."""
        result = switch_active_plan(temp_project_dir, "zzzzzzzz")
        assert result["success"] is False
        assert "error" in result

    def test_switch_no_active(self, temp_project_dir: str):
        """Switching when there is no active plan should work (first active)."""
        # Create registry with only paused/completed plans
        registry_path = Path(temp_project_dir) / ".ai" / "artifacts" / "registry.md"
        content = """# Active Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|

# Paused Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| p1a2b3c4 | ⏸️ | 2026-06-01 10:00 | Paused plan |

# Completed Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| c1d2e3f4 | ✅ | 2026-05-01 10:00 | Completed plan |
"""
        registry_path.write_text(content, encoding="utf-8")

        result = switch_active_plan(temp_project_dir, "p1a2b3c4")
        assert result["success"] is True
        assert result["new_active_uuid"] == "p1a2b3c4"

        registry = parse_registry(temp_project_dir)
        assert registry["active"][0]["uuid"] == "p1a2b3c4"

        # Should have been removed from paused
        assert not any(p["uuid"] == "p1a2b3c4" for p in registry["paused"])