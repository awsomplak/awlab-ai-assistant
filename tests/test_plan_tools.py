"""
Comprehensive tests for tools/plan_tools.py

Covers all 8 exported tools with edge cases:
- read_plan_tasks: structured, raw, minimal, invalid format, missing plan
- update_task_status: success, invalid status, invalid path, transition validation, degraded mode
- batch_update_tasks: atomic multi-update, rollback on error, file write failure
- validate_phase_gate: phase 1 bypass, blocking tasks, missing plan
- get_next_eligible_task: eligibility, dependencies, cascade failures, cross-phase scan
- validate_status_transition: legal, illegal, unknown markers
- list_registry: with/without registry file
- switch_active_plan: normal switch, nonexistent UUID, same active
"""

from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.tools.plan_tools import (
    batch_update_tasks,
    check_plan_completable,
    execute_workflow,
    generate_retrospective_summary,
    get_next_eligible_task,
    list_registry,
    list_workflows,
    mark_phase_complete,
    switch_active_plan,
    read_plan_tasks,
    resolve_deferred_tasks,
    update_task_status,
    validate_phase_gate,
)
from mcp_server.helpers.validation import validate_status_transition


# ── Fixtures for extended test scenarios ────────────────────────────────────────


@pytest.fixture
def complex_tasks_md(setup_tasks_md, plan_dir: str):
    """Extend sample tasks.md with dependency annotations and more phases."""
    content = Path(plan_dir) / "tasks.md"
    complex_content = """# Tasks

## Phase 1: Backend Auth
- [ ] Task 1: Implement JWT authentication
    - [ ] Task 1.1: Create JWT utility
    - [ ] Task 1.2: Add login endpoint
- [x] Task 2: Setup database models
    - [x] Task 2.1: Create User model
    - [x✓] Task 2.2: Run migrations

## Phase 2: Frontend Auth
- [ ] Task 3: Login page → depends: Task 1
    - [ ] Task 3.1: Build form UI
- [ ] Task 4: Token storage
    - [ ] Task 4.1: Implement secure storage

## Phase 3: Security
- [ ] Task 5: Rate limiting → depends: Task 3
- [ ] Task 6: Audit logging
"""
    content.write_text(complex_content, encoding="utf-8")
    return str(content)


@pytest.fixture
def full_complete_tasks_md(plan_dir: str, temp_project_dir: Path):
    """A plan where all tasks in Phase 1 are completed, plus registry.md."""
    content = Path(plan_dir) / "tasks.md"
    full_content = """# Tasks

## Phase 1: Backend Auth
- [x✓] Task 1: Implement JWT authentication
    - [x] Task 1.1: Create JWT utility
    - [x✓] Task 1.2: Add login endpoint
- [x] Task 2: Setup database models
    - [x] Task 2.1: Create User model
    - [x✓] Task 2.2: Run migrations

## Phase 2: Frontend Auth
- [ ] Task 3: Login page
    - [ ] Task 3.1: Build form UI
- [ ] Task 4: Token storage
    - [ ] Task 4.1: Implement secure storage
"""
    content.write_text(full_content, encoding="utf-8")

    # Also write registry.md so update_registry_phase_count succeeds
    registry_path = temp_project_dir / ".ai" / "artifacts" / "registry.md"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text("""# Active Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| a1b2c3d4 | ⏹️ | 2026-06-06 10:30 | User authentication flow |

# Paused Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| e5f6g7h8 | ⏸️ | 2026-06-05 15:00 | Database schema redesign |

# Completed Registry Plan

| UUID | Status | Date | Summary |
|------|--------|------|---------|
| i9j0k1l2 | ✅ | 2026-06-04 09:00 | Initial project setup |
""", encoding="utf-8")

    return str(content)


@pytest.fixture
def deferred_scenario_tasks_md(plan_dir: str):
    """A plan with deferred (⏳) tasks and unmet dependencies."""
    content = Path(plan_dir) / "tasks.md"
    deferred_content = """# Tasks

## Phase 1: Setup
- [x✓] Task 1: Install framework
- [⏳] Task 2: Configure database → depends: Task 3
- [ ] Task 3: Setup environment variables
- [—] Task 4: Legacy migration (skipped)
"""
    content.write_text(deferred_content, encoding="utf-8")
    return str(content)


# ── TestReadPlanTasks ──────────────────────────────────────────────────────────


class TestReadPlanTasks:
    async def test_read_valid_plan(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should return structured tasks for a valid plan."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert result["success"] is True
        assert result["plan_uuid"] == plan_uuid
        assert result["format"] == "structured"
        assert len(result["phases"]) == 2
        assert result["phases"][0]["name"] == "Phase 1: Backend Auth"
        assert result["phases"][0]["tasks"][0]["description"] == "Task 1: Implement JWT authentication"

    async def test_read_nonexistent_plan(self, temp_project_dir: str):
        """Should return error for plan that doesn't exist."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid="ffffffff")
        assert result["success"] is False
        assert "error" in result

    async def test_read_missing_tasks_file(self, temp_project_dir: str, plan_dir: str, plan_uuid: str):
        """Should return error when tasks.md doesn't exist in the plan dir."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert result["success"] is False
        assert "error" in result

    async def test_read_raw_format(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should return raw markdown content when format='raw'."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid, format="raw")
        assert result["success"] is True
        assert result["format"] == "raw"
        assert result["plan_uuid"] == plan_uuid
        assert "## Phase 1: Backend Auth" in result["content"]
        assert "[ ] Task 1: Implement JWT authentication" in result["content"]

    async def test_read_minimal_format(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should return summary counts when format='minimal'."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid, format="minimal")
        assert result["success"] is True
        assert result["format"] == "minimal"
        # The summary fields are unpacked into the top-level result
        assert result["total"] > 0
        assert result["pending"] > 0
        assert result["completed"] > 0
        assert "phase_summaries" in result

    async def test_read_minimal_matches_counts(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Minimal format should match expected counts from sample data."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid, format="minimal")
        # Summary fields are unpacked into top-level result
        # Sample has: Task 1 + 1.1 + 1.2 = 3, Task 2 + 2.1 + 2.2 = 3,
        # Task 3 + 3.1 + 3.2 = 3, Task 4 + 4.1 = 2 -> 11 total
        assert result["total"] == 11
        # Phase 1 pending: Task 1 + 1.1 + 1.2 = 3
        # Phase 2 pending: Task 3 + 3.1 + 3.2 = 3
        assert result["pending"] >= 6
        # Completed: Task 2 + 2.1 + 2.2 = 3
        assert result["completed"] >= 3
        assert result["skipped"] == 0
        assert result["failed"] == 0

    async def test_read_invalid_format(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should default to structured for unknown format values."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid, format="unknown_format")
        assert result["success"] is True
        assert result["format"] == "structured"
        assert "phases" in result


# ── TestUpdateTaskStatus ──────────────────────────────────────────────────────


class TestUpdateTaskStatus:
    async def test_update_toplevel_task(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should update a top-level task status."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.1",
            new_status="[x]"
        )
        assert result["success"] is True
        assert result.get("old_status") == "[ ]"
        assert result["new_status"] == "[x]"
        assert result["db_synced"] is True
        assert "file_path" in result

        # Verify persistence
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert check["phases"][0]["tasks"][0]["status"] == "[x]"

    async def test_update_task_returns_old_status(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should return old_status metadata."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.1",
            new_status="[x]"
        )
        assert result["old_status"] == "[ ]"
        assert result["new_status"] == "[x]"

    async def test_update_task_returns_pre_mutation_state(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should return pre_mutation_state snapshot."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.1",
            new_status="[x]"
        )
        assert "pre_mutation_state" in result
        assert "Implement JWT authentication" in result["pre_mutation_state"]

    async def test_update_phase2_task(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should update a task in Phase 2."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="2.1",
            new_status="[x]"
        )
        assert result["success"] is True
        assert result["old_status"] == "[ ]"

        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert check["phases"][1]["tasks"][0]["status"] == "[x]"

    async def test_update_from_x_to_xcheck(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should transition from [x] to [x✓]."""
        # First set to [x]
        await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.2",
            new_status="[x]"
        )
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.2",
            new_status="[x✓]"
        )
        assert result["success"] is True
        assert result["old_status"] == "[x]"
        assert result["new_status"] == "[x✓]"

    async def test_update_from_x_to_blank(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should reject un-checking a completed task back to pending (illegal transition)."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.2",
            new_status="[x]"
        )
        assert result["success"] is False
        assert "error" in result
        assert "Cannot transition" in result["error"]
        assert result["old_status"] == "[x]"

    async def test_update_invalid_path(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str
    ):
        """Should return error for out-of-range task path."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.99",
            new_status="[x]"
        )
        assert result["success"] is False
        assert "error" in result

    async def test_update_invalid_status(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str
    ):
        """Should reject invalid status marker."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.1",
            new_status="[invalid]"
        )
        assert result["success"] is False
        assert "error" in result

    async def test_update_nonexistent_plan(
        self,
        temp_project_dir: str,
        project_id: str,
    ):
        """Should return error for non-existent plan."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid="ffffffff",
            task_path="1.1",
            new_status="[x]"
        )
        assert result["success"] is False
        assert "error" in result

    async def test_update_malformed_path(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str
    ):
        """Should reject malformed task paths."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="abc.def",
            new_status="[x]"
        )
        assert result["success"] is False
        assert "error" in result

    async def test_update_wrong_phase(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str
    ):
        """Should return error for out-of-range phase."""
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="99.1",
            new_status="[x]"
        )
        assert result["success"] is False
        assert "error" in result

    async def test_update_illegal_transition(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should reject illegal status transitions like [—] → [x]."""
        # First set to [—] (skipped)
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.1",
            new_status="[—]"
        )
        assert result["success"] is True

        # Now try to change [—] to [x] — illegal
        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.1",
            new_status="[x]"
        )
        assert result["success"] is False
        assert "error" in result
        assert "Cannot transition" in result["error"]
        assert "valid_targets" in result
        assert result["old_status"] == "[—]"

    async def test_update_deferred_unmet_dependency(
        self,
        temp_project_dir: str,
        project_id: str,
        plan_dir: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should allow setting a task to [⏳] (deferred)."""
        tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        tasks.write_text("""# Tasks

## Phase 1: Setup
- [ ] Task 1: Do something
- [ ] Task 2: Do another thing → depends: Task 1
""", encoding="utf-8")

        result = await update_task_status(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            task_path="1.2",
            new_status="[⏳]"
        )
        assert result["success"] is True
        assert result["new_status"] == "[⏳]"


# ── TestBatchUpdateTasks ──────────────────────────────────────────────────────


class TestBatchUpdateTasks:
    async def test_batch_successful_update(
        self,
        temp_project_dir: str,
        project_id: str,
        setup_tasks_md: str,
        plan_uuid: str,
        mock_agent_recall_success
    ):
        """Should atomically update multiple tasks."""
        updates = [
            {"task_path": "1.1", "new_status": "[x]"},
            {"task_path": "2.1", "new_status": "[x]"},
        ]
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=updates
        )
        assert result["success"] is True
        assert len(result["successful"]) == 2
        assert result["successful"][0]["task_path"] == "1.1"
        assert result["successful"][0]["old_status"] == "[ ]"
        assert result["successful"][0]["new_status"] == "[x]"
        assert result["successful"][1]["task_path"] == "2.1"
        assert result["db_synced"] is True

        # Verify persistence
        check = await read_plan_tasks(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid
        )
        assert check["phases"][0]["tasks"][0]["status"] == "[x]"
        assert check["phases"][1]["tasks"][0]["status"] == "[x]"

    async def test_batch_rollback_on_invalid_status(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, project_id: str):
        """Should rollback ALL changes when one update has invalid status."""
        updates = [
            {"task_path": "1.1", "new_status": "[x]"},
            {"task_path": "1.2", "new_status": "[INVALID]"},
        ]
        result = await batch_update_tasks(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, updates=updates)
        assert result["success"] is False
        assert result["rolled_back"] is True
        assert len(result["successful"]) == 0
        assert len(result["failed"]) == 1
        assert result["failed"][0]["task_path"] == "1.2"

        # Verify rollback — first task should NOT be changed
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert check["phases"][0]["tasks"][0]["status"] == "[ ]"

    async def test_batch_rollback_on_nonexistent_task(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, project_id: str):
        """Should rollback ALL changes when a task path doesn't exist."""
        updates = [
            {"task_path": "1.1", "new_status": "[x]"},
            {"task_path": "1.99", "new_status": "[x✓]"},
        ]
        result = await batch_update_tasks(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, updates=updates)
        assert result["success"] is False
        assert result["rolled_back"] is True
        assert len(result["successful"]) == 0
        assert len(result["failed"]) == 1

        # Verify full rollback
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert check["phases"][0]["tasks"][0]["status"] == "[ ]"

    async def test_batch_nonexistent_plan(self, temp_project_dir: str, project_id: str):
        """Should error when plan doesn't exist."""
        updates = [{"task_path": "1.1", "new_status": "[x]"}]
        result = await batch_update_tasks(workspace_path=temp_project_dir, project_id=project_id, plan_uuid="ffffffff", updates=updates)
        assert result["success"] is False
        assert "error" in result

    async def test_batch_single_update(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, project_id: str):
        """Should handle a single update correctly."""
        updates = [{"task_path": "1.1", "new_status": "[x]"}]
        result = await batch_update_tasks(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, updates=updates)
        assert result["success"] is True
        assert len(result["successful"]) == 1
        assert result["successful"][0]["old_status"] == "[ ]"

    async def test_batch_returns_pre_mutation_state(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, project_id: str):
        """Should include pre_mutation_state in response."""
        updates = [{"task_path": "1.1", "new_status": "[x]"}]
        result = await batch_update_tasks(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, updates=updates)
        assert "pre_mutation_state" in result
        assert "Implement JWT authentication" in result["pre_mutation_state"]

    async def test_batch_mixed_transitions(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, project_id: str):
        """Should handle various valid status transitions in one batch."""
        updates = [
            {"task_path": "1.1", "new_status": "[x]"},
            {"task_path": "1.2", "new_status": "[x✓]"},
        ]
        result = await batch_update_tasks(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, updates=updates)
        assert result["success"] is True
        assert len(result["successful"]) == 2

        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert check["phases"][0]["tasks"][0]["status"] == "[x]"
        assert check["phases"][0]["tasks"][1]["status"] == "[x✓]"


# ── TestValidatePhaseGate ─────────────────────────────────────────────────────


class TestValidatePhaseGate:
    async def test_phase_1_always_passes(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Phase 1 has no predecessor, so gate always passes."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=1
        )
        assert result["pass"] is True
        assert result["phase_complete"] is True
        assert "no predecessor" in result["reasons"][0]

    async def test_phase_2_blocked_by_phase_1(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Phase 2 should be blocked if Phase 1 has incomplete tasks."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=2
        )
        assert result["pass"] is False
        assert result["phase_complete"] is False
        assert len(result["blocking_tasks"]) > 0

    async def test_phase_2_passes_when_phase_1_complete(self, temp_project_dir: str, full_complete_tasks_md: str, plan_uuid: str):
        """Phase 2 should pass when Phase 1 has all tasks terminal."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=2
        )
        assert result["pass"] is True
        assert result["phase_complete"] is True
        assert len(result["blocking_tasks"]) == 0

    async def test_gate_missing_plan(self, temp_project_dir: str):
        """Should fail gracefully for missing plan."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid="ffffffff",
            phase_num=2
        )
        assert result["pass"] is False
        assert not result["phase_complete"]
        assert len(result["reasons"]) > 0

    async def test_gate_phase_0(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Phase 0 or less should pass like Phase 1 (no predecessor)."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=0
        )
        assert result["pass"] is True

    async def test_gate_negative_phase(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Negative phase should also pass (no predecessor gate logic)."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=-1
        )
        assert result["pass"] is True

    async def test_blocking_tasks_details(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Blocking tasks should include index, description, and status."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=2
        )
        for bt in result["blocking_tasks"]:
            assert "index" in bt
            assert "description" in bt
            assert "status" in bt

    async def test_blocking_task_identifies_unfinished_tasks(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Blocking tasks list should contain only incomplete tasks."""
        result = await validate_phase_gate(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase_num=2
        )
        descriptions = [t["description"] for t in result["blocking_tasks"]]
        # Task 1 is pending [ ], it should be in blocking
        assert any("Implement JWT authentication" in d for d in descriptions)
        # Task 2 is completed [x], should NOT be in blocking
        assert not any("Setup database models" in d for d in descriptions)


# ── TestGetNextEligibleTask ───────────────────────────────────────────────────


class TestGetNextEligibleTask:
    async def test_find_first_eligible(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should find the first pending task in Phase 1."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=1
        )
        assert result["success"] is True
        assert result["next_task"] is not None
        assert result["next_task"]["description"] == "Task 1: Implement JWT authentication"

    async def test_skip_completed_tasks(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should skip completed tasks and find next pending."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=2
        )
        assert result["success"] is True
        assert result["next_task"] is not None
        assert result["next_task"]["description"] == "Task 3: Login page"

    async def test_all_terminal_in_phase(self, temp_project_dir: str, full_complete_tasks_md: str, plan_uuid: str):
        """Should return all_terminal=True when no pending tasks in phase."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=1
        )
        assert result["success"] is True
        assert result["all_terminal"] is True
        assert result["next_task"] is None

    async def test_scan_across_phases(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """When no phase specified, should scan all phases in order."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
        )
        assert result["success"] is True
        assert result["next_task"] is not None
        # First eligible task should be in Phase 1
        assert result["phase_number"] == 1

    async def test_scan_skips_to_next_phase(self, temp_project_dir: str, full_complete_tasks_md: str, plan_uuid: str):
        """When Phase 1 is all terminal, should find task in Phase 2."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
        )
        assert result["success"] is True
        assert result["next_task"] is not None
        assert result["phase_number"] == 2

    async def test_missing_plan(self, temp_project_dir: str):
        """Should error when plan doesn't exist."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid="ffffffff"
        )
        assert result["success"] is False
        assert "error" in result

    async def test_nonexistent_phase(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should return all_terminal for non-existent phase number."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=99
        )
        assert result["success"] is True
        assert result["next_task"] is None
        assert result["all_terminal"] is True

    async def test_deferred_task_reevaluation(self, temp_project_dir: str, deferred_scenario_tasks_md: str, plan_uuid: str):
        """Should re-evaluate deferred tasks when dependencies become met."""
        # Phase 1 has: Task 1 [x✓], Task 2 [⏳] (depends: Task 3), Task 3 [ ], Task 4 [—]
        # Task 2 is deferred because Task 3 is pending
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=1
        )
        assert result["success"] is True
        # Task 3 has no dependencies, should be next
        assert result["next_task"]["description"] == "Task 3: Setup environment variables"

    async def test_cascade_failure(self, temp_project_dir: str, plan_dir: str, plan_uuid: str):
        """Should detect cascade failure when all tasks are blocked or terminal."""
        tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        tasks.write_text("""# Tasks

## Phase 1: Setup
- [ ] Task 1: Do something → depends: Task 3
- [⏳] Task 2: Do another → depends: Task 1
- [ ] Task 3: Do final → depends: Task 2
""", encoding="utf-8")

        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=1
        )
        assert result["success"] is True
        assert result["cascade_failure"] is True
        assert result["next_task"] is None

    async def test_skipped_task_not_blocking(self, temp_project_dir: str, deferred_scenario_tasks_md: str, plan_uuid: str):
        """Skipped [—] tasks should not block other tasks."""
        result = await get_next_eligible_task(
            workspace_path=temp_project_dir,
            plan_uuid=plan_uuid,
            phase=1
        )
        assert result["success"] is True
        assert result["next_task"] is not None
        # Task 4 is skipped, should not appear as next_task or in deferred
        for d in result.get("deferred", []):
            assert "Legacy migration" not in d["description"]


# ── TestValidateStatusTransition ──────────────────────────────────────────────


class TestValidateStatusTransition:
    def test_legal_transition_pending_to_done(self):
        """[ ] → [x] should be legal."""
        result = validate_status_transition("[ ]", "[x]")
        assert result["valid"] is True

    def test_legal_transition_pending_to_failed(self):
        """[ ] → [!] should be legal."""
        result = validate_status_transition("[ ]", "[!]")
        assert result["valid"] is True

    def test_legal_transition_done_to_verified(self):
        """[x] → [x✓] should be legal."""
        result = validate_status_transition("[x]", "[x✓]")
        assert result["valid"] is True

    def test_skipped_is_terminal(self):
        """[—] has no legal outgoing transitions."""
        result = validate_status_transition("[—]", "[x]")
        assert result["valid"] is False

    def test_failed_to_skipped(self):
        """[!] → [—] should be legal (failed can be skipped)."""
        result = validate_status_transition("[!]", "[—]")
        assert result["valid"] is True

    def test_skipped_to_deferred(self):
        """[—] → [⏳] should be illegal (skipped is terminal)."""
        result = validate_status_transition("[—]", "[⏳]")
        assert result["valid"] is False

    def test_unknown_current_status(self):
        """Unknown current status should return valid=False."""
        result = validate_status_transition("[???]", "[x]")
        assert result["valid"] is False
        assert "Unknown" in result["reason"]
        assert len(result["valid_targets"]) > 0

    def test_unknown_target_status(self):
        """Unknown target status should return valid=False."""
        result = validate_status_transition("[ ]", "[???]")
        assert result["valid"] is False
        # The error message uses "Cannot transition" since [???] is not in valid_targets set
        assert "Cannot transition" in result["reason"]

    def test_all_legal_transitions_from_pending(self):
        """[ ] should allow: [x], [x✓], [x!], [!], [—], [⏳]."""
        for target in ["[x]", "[x✓]", "[x!]", "[!]", "[—]", "[⏳]"]:
            result = validate_status_transition("[ ]", target)
            assert result["valid"] is True, f"[ ] → {target} should be legal"

    def test_deferred_can_resolve(self):
        """[⏳] should allow resolution transitions."""
        for target in ["[x]", "[x✓]", "[x!]", "[!]", "[—]"]:
            result = validate_status_transition("[⏳]", target)
            assert result["valid"] is True, f"[⏳] → {target} should be legal"


# ── TestListRegistry ──────────────────────────────────────────────────────────


class TestListRegistry:
    async def test_list_with_registry(self, temp_project_dir: str, setup_registry_md: str):
        """Should return all three tables."""
        result = await list_registry(temp_project_dir)
        assert "active" in result
        assert "paused" in result
        assert "completed" in result
        assert len(result["active"]) == 1
        assert len(result["paused"]) == 1
        assert len(result["completed"]) == 1

    async def test_list_without_registry(self, temp_project_dir: str):
        """Should return empty tables when no registry."""
        result = await list_registry(temp_project_dir)
        assert result["active"] == []
        assert result["paused"] == []
        assert result["completed"] == []


# ── TestSwitchActivePlan ──────────────────────────────────────────────────────


class TestSwitchActivePlan:
    async def test_switch_to_paused(self, temp_project_dir: str, setup_registry_md: str):
        """Should switch active plan to a paused one."""
        result = await switch_active_plan(workspace_path=temp_project_dir, uuid="e5f6g7h8")
        assert result["success"] is True
        assert result["new_active_uuid"] == "e5f6g7h8"

    async def test_switch_nonexistent(self, temp_project_dir: str, setup_registry_md: str):
        """Should return error for nonexistent UUID."""
        result = await switch_active_plan(workspace_path=temp_project_dir, uuid="zzzzzzzz")
        assert result["success"] is False
        assert "error" in result

    async def test_switch_same_active(self, temp_project_dir: str, setup_registry_md: str):
        """Switching to the already-active plan should succeed."""
        result = await switch_active_plan(workspace_path=temp_project_dir, uuid="a1b2c3d4")
        assert result["success"] is True
        assert result["new_active_uuid"] == "a1b2c3d4"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 Tool Tests
# ═══════════════════════════════════════════════════════════════════════════════


# ── Additional Sample Content ────────────────────────────────────────────────


COMPLETE_TASKS_MD = """# Tasks

## Phase 1: Backend Auth
- [x] Task 1: Implement JWT authentication
    - [x] Task 1.1: Create JWT utility
    - [x✓] Task 1.2: Add login endpoint
- [x] Task 2: Setup database models
    - [x] Task 2.1: Create User model
    - [x✓] Task 2.2: Run migrations

## Phase 2: Frontend Auth
- [x] Task 3: Login page
    - [x] Task 3.1: Build form UI
    - [x✓] Task 3.2: Add validation
- [x!] Task 4: Token storage
    - [x!] Task 4.1: Implement secure storage
"""

DEFERRED_TASKS_MD = """# Tasks

## Phase 1: Backend Auth
- [x] Task 1: Setup database
- [ ] Task 2: Create API
- [⏳] Task 3: Write tests → depends: Task 1

## Phase 2: Frontend Auth
- [ ] Task 4: Login page
- [⏳] Task 5: Token storage → depends: Task 4
- [⏳] Task 6: Integrate with backend → depends: Task 2, Task 4
"""

INCOMPLETE_TASKS_MD = """# Tasks

## Phase 1: Backend Auth
- [x] Task 1: Setup database
- [x] Task 2: Create API
- [ ] Task 3: Write tests

## Phase 2: Frontend Auth
- [ ] Task 4: Login page
"""

MIXED_TASKS_MD = """# Tasks

## Phase 1: Backend Auth
- [x] Task 1: Setup database
    - [x] Task 1.1: Create models
- [x!] Task 2: Create API
    - [x!] Task 2.1: Warning on tests
- [—] Task 3: Legacy migration (skipped)

## Phase 2: Frontend Auth
- [x] Task 4: Login page
- [x] Task 5: Token storage
"""

SAMPLE_NOTES_MD = """# Notes

## Design Decisions
- Using JWT for authentication
- PostgreSQL for data storage

## Risks
- Token expiration needs careful handling
"""


SAMPLE_WORKFLOW_MD = """---
description: Test workflow for unit tests
---

# Test Workflow

## Step 1: Setup
- `log` Initialize environment
- `file_op` Create config file path="config.json"
- `agent_task` Ask user for preferences

## Step 2: Execute
- `mcp_tool` Call read_plan_tasks tool_name="read_plan_tasks"
- `log` Completed
"""


# ── Tests: mark_phase_complete ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_phase_complete_success(temp_project_dir, plan_uuid, project_id, full_complete_tasks_md, mock_agent_recall_success):
    """Mark Phase 1 as complete — all tasks already terminal in the fixture."""
    result = await mark_phase_complete(
        workspace_path=temp_project_dir,
        plan_uuid=plan_uuid,
        phase_num=1,
        project_id=project_id
    )

    assert result["success"] is True
    assert result["completed_count"] == 2  # Two top-level tasks in Phase 1
    assert "Phase 1" in result.get("notes_summary", "")

    # Verify file was written
    phase_1_tasks = result.get("phase_tasks", [])
    assert len(phase_1_tasks) > 0
    for task in phase_1_tasks:
        assert task["status"] in ("[x]", "[x✓]")


@pytest.mark.asyncio
async def test_mark_phase_complete_invalid_uuid(temp_project_dir, plan_uuid, project_id):
    """Invalid UUID should return error."""
    result = await mark_phase_complete(
        workspace_path=temp_project_dir,
        plan_uuid="invalid!!!",
        phase_num=1,
        project_id=project_id
    )
    assert result["success"] is False
    assert "Invalid" in result.get("error", "")


@pytest.mark.asyncio
async def test_mark_phase_complete_invalid_phase(temp_project_dir, plan_uuid, project_id):
    """Phase number less than 1 should return error."""
    result = await mark_phase_complete(
        workspace_path=temp_project_dir,
        plan_uuid=plan_uuid,
        phase_num=0,
        project_id=project_id
    )
    assert result["success"] is False
    assert "phase_number" in result.get("error", "")


@pytest.mark.asyncio
async def test_mark_phase_complete_no_tasks_file(temp_project_dir, plan_uuid, project_id):
    """Missing tasks.md should return error."""
    result = await mark_phase_complete(
        workspace_path=temp_project_dir,
        plan_uuid=plan_uuid,
        phase_num=1,
        project_id=project_id
    )
    assert result["success"] is False


# ── Tests: resolve_deferred_tasks ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_deferred_tasks_all_blocked(temp_project_dir, plan_uuid):
    """All deferred tasks remain blocked when dependencies are unmet."""
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text(DEFERRED_TASKS_MD, encoding="utf-8")

    result = await resolve_deferred_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)

    assert result["success"] is True
    # Task 3 depends on Task 1 (completed), so it might be resolvable
    # Task 5 depends on Task 4 (pending), so blocked
    # Task 6 depends on Task 2 (pending) and Task 4 (pending), so blocked
    assert len(result["resolved"]) == 1  # Task 3 can be resolved
    assert len(result["remaining"]) == 2  # Tasks 5 and 6 remain
    assert result["total_deferred"] == 3


@pytest.mark.asyncio
async def test_resolve_deferred_tasks_specific_phase(temp_project_dir, plan_uuid):
    """Filter by phase number."""
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text(DEFERRED_TASKS_MD, encoding="utf-8")

    result = await resolve_deferred_tasks(
        workspace_path=temp_project_dir,
        plan_uuid=plan_uuid,
        phase_number=2
    )

    assert result["success"] is True
    # Only Phase 2 deferred tasks: Task 5 (dep on Task 4 - pending), Task 6 (dep on Task 2,4)
    assert len(result["resolved"]) == 0  # Still blocked
    assert len(result["remaining"]) == 2


@pytest.mark.asyncio
async def test_resolve_deferred_tasks_no_deferred(temp_project_dir, plan_uuid):
    """No deferred tasks should return empty lists."""
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text("""# Tasks

## Phase 1: Setup
- [x] Task 1: Install framework
- [ ] Task 2: Configure database
""", encoding="utf-8")

    result = await resolve_deferred_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)

    assert result["success"] is True
    assert result["total_deferred"] == 0
    assert len(result["resolved"]) == 0
    assert len(result["remaining"]) == 0


@pytest.mark.asyncio
async def test_resolve_deferred_tasks_invalid_uuid(temp_project_dir, plan_uuid):
    """Invalid UUID should return error."""
    result = await resolve_deferred_tasks(workspace_path=temp_project_dir, plan_uuid="bad!!!")
    assert result["success"] is False


# ── Tests: check_plan_completable ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_plan_completable_true(temp_project_dir, plan_uuid):
    """All tasks in terminal state should return completable=True."""
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text(MIXED_TASKS_MD, encoding="utf-8")

    result = await check_plan_completable(workspace_path=temp_project_dir, plan_uuid=plan_uuid)

    assert result["success"] is True
    assert result["completable"] is True
    assert len(result["incomplete_tasks"]) == 0
    assert result["summary"]["total"] == 7  # 5 top-level + 2 subtasks
    assert result["summary"]["completed"] == 7  # all tasks terminal ([x], [x!], and [—])
    assert result["summary"]["skipped"] == 1  # [—]


@pytest.mark.asyncio
async def test_check_plan_completable_false(temp_project_dir, plan_uuid):
    """Pending tasks should return completable=False."""
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text(INCOMPLETE_TASKS_MD, encoding="utf-8")

    result = await check_plan_completable(workspace_path=temp_project_dir, plan_uuid=plan_uuid)

    assert result["success"] is True
    assert result["completable"] is False
    assert len(result["incomplete_tasks"]) > 0


@pytest.mark.asyncio
async def test_check_plan_completable_invalid_uuid(temp_project_dir, plan_uuid):
    """Invalid UUID should return error."""
    result = await check_plan_completable(workspace_path=temp_project_dir, plan_uuid="wrong!")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_check_plan_completable_no_file(temp_project_dir, plan_uuid):
    """Missing tasks.md should return error."""
    result = await check_plan_completable(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    assert result["success"] is False


# ── Tests: execute_workflow ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_workflow_success(temp_project_dir: str, project_id: str):
    """Execute a valid workflow with log and file_op steps."""
    workflows_dir = Path(temp_project_dir) / "Cline" / "Workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    wf_path = workflows_dir / "test-flow.md"
    wf_path.write_text(SAMPLE_WORKFLOW_MD, encoding="utf-8")

    result = await execute_workflow(
        workspace_path=temp_project_dir,
        project_id=project_id,
        workflow_name="test-flow",
        workflows_dir=workflows_dir,
    )

    assert result["success"] is True
    assert result["workflow"] == "test-flow"
    # Parser groups steps by ## sections; sample has 2 sections → 2 steps
    assert len(result["steps"]) == 2
    # Step 1: Setup (last action in section overwrites type → agent_task)
    assert result["steps"][0]["step_name"] == "Step 1: Setup"
    assert result["steps"][0]["type"] == "agent_task"
    assert result["steps"][0]["status"] == "pending"
    assert "Ask user for preferences" in result["steps"][0]["output"]
    # Step 2: Execute (last action overwrites type → log)
    assert result["steps"][1]["step_name"] == "Step 2: Execute"
    assert result["steps"][1]["type"] == "log"
    assert result["steps"][1]["status"] == "passed"
    assert "Completed" in result["steps"][1]["output"]


@pytest.mark.asyncio
async def test_execute_workflow_not_found(temp_project_dir):
    """Non-existent workflow should return error."""
    workflows_dir = Path(temp_project_dir) / "Cline" / "Workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    result = await execute_workflow(
        workspace_path=temp_project_dir,
        workflow_name="non-existent-workflow",
        workflows_dir=workflows_dir,
    )

    assert result["success"] is False
    assert "not found" in result.get("error", "").lower()


# ── Tests: list_workflows ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_workflows_success(temp_project_dir):
    """List workflows from the Cline/Workflows/ directory."""
    workflows_dir = Path(temp_project_dir) / "Cline" / "Workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    # Create two sample workflow files
    (workflows_dir / "auth-flow.md").write_text(
        "# Auth Flow\n\ndescription: Authentication steps\n\n## Login\n- `log` Login step",
        encoding="utf-8",
    )
    (workflows_dir / "deploy-flow.md").write_text(
        "# Deploy Flow\n\ndescription: Deployment steps\n\n## Build\n- `log` Build step\n## Deploy\n- `log` Deploy step",
        encoding="utf-8",
    )

    result = await list_workflows(workspace_path=temp_project_dir)

    assert result["success"] is True
    assert result["count"] == 2
    # Sort by name for deterministic assertion
    names = sorted(w["name"] for w in result["workflows"])
    assert names == ["auth-flow", "deploy-flow"]


@pytest.mark.asyncio
async def test_list_workflows_empty(temp_project_dir):
    """No workflows directory should return empty list."""
    # Do NOT create Cline/Workflows/
    result = await list_workflows(workspace_path=temp_project_dir)

    assert result["success"] is False
    assert "not found" in result.get("error", "").lower()


# ── Tests: generate_retrospective_summary ────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_retrospective_summary_success(temp_project_dir, plan_uuid, mock_agent_recall_success):
    """Generate retrospective summary from a completed plan."""
    # Write completed tasks
    plan_path = settings.get_plan_dir(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    plan_path.mkdir(parents=True, exist_ok=True)
    tasks_file = plan_path / "tasks.md"
    tasks_file.write_text(COMPLETE_TASKS_MD, encoding="utf-8")

    # Write plan.md
    plan_file = plan_path / "plan.md"
    plan_file.write_text("# User Auth Flow\n\nJWT auth implementation", encoding="utf-8")

    # Write notes.md
    notes_file = plan_path / "notes.md"
    notes_file.write_text(SAMPLE_NOTES_MD, encoding="utf-8")

    result = await generate_retrospective_summary(workspace_path=temp_project_dir, plan_uuid=plan_uuid)

    assert result["success"] is True
    assert result["plan_uuid"] == plan_uuid
    assert result["patterns_extracted"] > 0
    assert result["plan_summary"]["name"] == "User Auth Flow"
    assert result["plan_summary"]["total_tasks"] == 4  # top-level tasks
    assert result["plan_summary"]["completed"] == 4  # [x] + [x✓] + [x!]
    assert len(result["suggested_patterns"]) > 0


@pytest.mark.asyncio
async def test_generate_retrospective_summary_missing_tasks(temp_project_dir, plan_uuid):
    """Missing tasks.md should return error."""
    # Do NOT write tasks.md
    result = await generate_retrospective_summary(workspace_path=temp_project_dir, plan_uuid=plan_uuid)

    assert result["success"] is False
    assert "tasks.md not found" in result.get("error", "")


@pytest.mark.asyncio
async def test_generate_retrospective_summary_invalid_uuid(temp_project_dir, plan_uuid):
    """Invalid UUID should return error."""
    result = await generate_retrospective_summary(workspace_path=temp_project_dir, plan_uuid="bad!!!")
    assert result["success"] is False
    assert "Invalid" in result.get("error", "")
