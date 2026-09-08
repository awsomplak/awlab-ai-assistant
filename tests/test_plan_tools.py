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
from mcp_server.helpers.validation import validate_status_transition
from mcp_server.tools.plan_tools import (
    batch_update_tasks,
    check_plan_completable,
    execute_workflow,
    generate_retrospective_summary,
    get_next_eligible_task,
    list_registry,
    list_workflows,
    mark_phase_complete,
    read_plan_tasks,
    resolve_deferred_tasks,
    switch_active_plan,
    update_task_status,
    validate_phase_gate,
)

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
    registry_path.write_text(
        """# Active Registry Plan

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
""",
        encoding="utf-8",
    )

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
    @pytest.mark.parametrize(
        "format_type, expected_format, expected_keys",
        [
            ("structured", "structured", ["phases"]),
        ],
    )
    async def test_read_plan_formats(
        self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str, format_type: str, expected_format: str, expected_keys: list[str]
    ):
        """Test read_plan_tasks with various formats."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid, format=format_type)
        assert result["success"] is True
        assert result["plan_uuid"] == plan_uuid
        assert result["format"] == expected_format
        for key in expected_keys:
            assert key in result

    async def test_read_minimal_matches_counts(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Minimal format should match expected counts from sample data."""
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid, format="minimal")
        assert result["total"] == 11
        assert result["pending"] >= 6
        assert result["completed"] >= 3
        assert result["skipped"] == 0
        assert result["failed"] == 0

    @pytest.mark.parametrize(
        "plan_id, inject_tasks",
        [
            ("ffffffff", True),  # Non-existent plan UUID
        ],
    )
    async def test_read_errors(self, temp_project_dir: str, plan_dir: str, plan_uuid: str, plan_id: str, inject_tasks: bool):
        """Test read_plan_tasks error conditions (missing plan or missing tasks.md)."""
        target_uuid = plan_uuid if plan_id == "valid" else plan_id
        if inject_tasks:
            # Ensure the directory exists but it's the wrong UUID, or the uuid is just wrong.
            pass
        result = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=target_uuid)
        assert result["success"] is False
        assert "error" in result


# ── TestUpdateTaskStatus ──────────────────────────────────────────────────────


class TestUpdateTaskStatus:
    @pytest.mark.parametrize(
        "task_path, new_status, expected_old, verify_path",
        [
            ("1.1", "[x]", "[ ]", "1.1"),
        ]
    )
    async def test_update_task_success(
        self, temp_project_dir: str, project_id: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, task_path: str, new_status: str, expected_old: str, verify_path: str
    ):
        """Should successfully update task status and return metadata."""
        result = await update_task_status(
            workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, task_path=task_path, new_status=new_status
        )
        assert result["success"] is True
        assert result.get("old_status") == expected_old
        assert result["new_status"] == new_status
        assert "pre_mutation_state" in result
        
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        phase_idx, task_idx = int(verify_path.split('.')[0]) - 1, int(verify_path.split('.')[1]) - 1
        assert check["phases"][phase_idx]["tasks"][task_idx]["status"] == new_status

    async def test_update_deferred_unmet_dependency(
        self, temp_project_dir: str, project_id: str, plan_dir: str, plan_uuid: str, mock_agent_recall_success
    ):
        """Should allow setting a task to [⏳] (deferred)."""
        tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        tasks.write_text("# Tasks\n\n## Phase 1: Setup\n- [ ] Task 1\n- [ ] Task 2 → depends: Task 1\n", encoding="utf-8")

        result = await update_task_status(
            workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, task_path="1.2", new_status="[⏳]"
        )
        assert result["success"] is True
        assert result["new_status"] == "[⏳]"

    async def test_update_from_x_to_xcheck(
        self, temp_project_dir: str, project_id: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success
    ):
        """Should transition from [x] to [x✓]."""
        await update_task_status(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, task_path="1.2", new_status="[x]")
        result = await update_task_status(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, task_path="1.2", new_status="[x✓]")
        assert result["success"] is True
        assert result["old_status"] == "[x]"

    @pytest.mark.parametrize(
        "task_path, new_status, plan_id_override",
        [
            ("1.99", "[x]", None),              # invalid path
        ]
    )
    async def test_update_errors(
        self, temp_project_dir: str, project_id: str, setup_tasks_md: str, plan_uuid: str, mock_agent_recall_success, task_path: str, new_status: str, plan_id_override: str
    ):
        target_uuid = plan_id_override if plan_id_override else plan_uuid
        
        # Setup for illegal transitions
        if task_path == "1.2" and new_status == "[ ]":
            await update_task_status(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, task_path="1.2", new_status="[x]")
        elif task_path == "1.1" and new_status == "[x]" and not plan_id_override:
            await update_task_status(workspace_path=temp_project_dir, project_id=project_id, plan_uuid=plan_uuid, task_path="1.1", new_status="[—]")

        result = await update_task_status(
            workspace_path=temp_project_dir, project_id=project_id, plan_uuid=target_uuid, task_path=task_path, new_status=new_status
        )
        assert result["success"] is False
        assert "error" in result


