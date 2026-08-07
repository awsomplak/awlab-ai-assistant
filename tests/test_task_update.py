"""
Tests for the consolidated task_update surface (Phase 11).

Covers the task_update guarantees:
- multi-level N-level dotted paths (phase.task.subtask) for update + read
- auto-create missing task (with description) + missing phase chain
- atomic single-write + rollback on any failed update
- internal transition validation returning valid_targets
- executed / skipped / created trace on the batch response
"""

from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.helpers import create_task_in_md, get_task_status, parse_tasks_md, update_task_status_in_md
from mcp_server.tools.plan_tools import batch_update_tasks, read_plan_tasks

NESTED_TASKS_MD = """# Tasks

## Phase 1: Backend
- [ ] Task 1: Implement auth
    - [ ] Task 1.1: Create JWT util
        - [ ] Task 1.1.1: Sign helper
    - [ ] Task 1.2: Login endpoint
- [ ] Task 2: Setup database

## Phase 2: Frontend
- [ ] Task 3: Login page
"""


# ── Multi-level path resolution (pure helpers) ──────────────────────────────


class TestMultiLevelPaths:
    def test_update_innermost_subtask(self):
        """Updating 1.1.1.1 (innermost) changes only that task."""
        updated = update_task_status_in_md(NESTED_TASKS_MD, "1.1.1.1", "[x]")
        assert updated is not None
        assert get_task_status(updated, "1.1.1.1") == "[x]"
        # Siblings + parents untouched
        parsed = parse_tasks_md(updated)
        t1 = parsed["phases"][0]["tasks"][0]
        assert t1["status"] == "[ ]"
        assert t1["subtasks"][0]["status"] == "[ ]"
        assert t1["subtasks"][0]["subtasks"][0]["status"] == "[x]"

    def test_update_mid_subtask(self):
        """Updating 1.1.2 (Task 1.1) leaves 1.1.1.1 untouched."""
        updated = update_task_status_in_md(NESTED_TASKS_MD, "1.1.2", "[x]")
        assert updated is not None
        assert get_task_status(updated, "1.1.2") == "[x]"
        assert get_task_status(updated, "1.1.1.1") == "[ ]"

    def test_parse_tasks_md_includes_path(self):
        """parse_tasks_md exposes the resolvable dotted path per task."""
        parsed = parse_tasks_md(NESTED_TASKS_MD)
        t1 = parsed["phases"][0]["tasks"][0]
        assert t1["path"] == "1.1"
        assert t1["subtasks"][0]["path"] == "1.1.1"
        assert t1["subtasks"][0]["subtasks"][0]["path"] == "1.1.1.1"
        assert t1["subtasks"][1]["path"] == "1.1.2"
        assert parsed["phases"][0]["tasks"][1]["path"] == "1.2"
        # Every path resolves back to a real line
        for p in [t1["path"], t1["subtasks"][0]["path"], t1["subtasks"][0]["subtasks"][0]["path"]]:
            assert get_task_status(NESTED_TASKS_MD, p) is not None

    def test_nonexistent_nested_path(self):
        """Non-existent nested path returns None."""
        assert update_task_status_in_md(NESTED_TASKS_MD, "1.1.9.9", "[x]") is None
        assert get_task_status(NESTED_TASKS_MD, "2.99") is None


# ── Auto-create (pure helper) ────────────────────────────────────────────────


class TestCreateTaskInMd:
    def test_create_missing_task(self):
        """Create a missing top-level task under an existing phase."""
        updated, path = create_task_in_md(NESTED_TASKS_MD, "2.2", description="New frontend task", new_status="[x]")
        assert path == "2.2"
        assert updated is not None
        assert get_task_status(updated, "2.2") == "[x]"
        assert "New frontend task" in updated

    def test_create_missing_phase(self):
        """Create a missing phase + its first task."""
        updated, path = create_task_in_md(NESTED_TASKS_MD, "3.1", description="Phase 3 task", new_status="[ ]")
        assert path == "3.1"
        assert updated is not None
        assert "## Phase 3" in updated
        assert get_task_status(updated, "3.1") == "[ ]"

    def test_create_missing_parent_chain(self):
        """Create nested path when parents are missing (1.3.1 needs Task 1.3)."""
        updated, path = create_task_in_md(NESTED_TASKS_MD, "1.3.1", description="Deep task", new_status="[x]")
        assert path == "1.3.1"
        assert updated is not None
        # Parent chain created too
        assert get_task_status(updated, "1.3") == "[ ]"
        assert get_task_status(updated, "1.3.1") == "[x]"

    def test_create_existing_returns_none(self):
        """Creating an already-existing task returns (None, None)."""
        assert create_task_in_md(NESTED_TASKS_MD, "1.1", description="exists") == (None, None)

    def test_create_malformed_path(self):
        """Malformed path returns (None, None)."""
        assert create_task_in_md(NESTED_TASKS_MD, "abc", description="x") == (None, None)
        assert create_task_in_md(NESTED_TASKS_MD, "1.1", description="") == (None, None)


# ── Batch update via task_update (integration) ──────────────────────────────


@pytest.fixture
def nested_plan(temp_project_dir: str, plan_dir: str, plan_uuid: str) -> str:
    """Write a nested tasks.md into the plan dir."""
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.write_text(NESTED_TASKS_MD, encoding="utf-8")
    return str(tasks)


class TestTaskUpdateBatch:
    async def test_update_multi_level(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """task_update can address a nested subtask via 1.1.1.1."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[{"task_path": "1.1.1.1", "new_status": "[x]"}],
        )
        assert result["success"] is True
        assert result["executed"] == [{"task_path": "1.1.1.1", "old_status": "[ ]", "new_status": "[x]"}]
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        assert check["phases"][0]["tasks"][0]["subtasks"][0]["subtasks"][0]["status"] == "[x]"

    async def test_auto_create_with_description(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """A missing task with a description is auto-created."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[{"task_path": "2.2", "new_status": "[x]", "description": "New frontend task"}],
        )
        assert result["success"] is True
        assert result["created"] == [{"task_path": "2.2", "description": "New frontend task", "new_status": "[x]"}]
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        phase2_tasks = check["phases"][1]["tasks"]
        assert any("New frontend task" in t["description"] for t in phase2_tasks)

    async def test_auto_create_phase_chain(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """A missing phase is created along with the task."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[{"task_path": "3.1", "new_status": "[ ]", "description": "Phase 3 task"}],
        )
        assert result["success"] is True
        assert result["created"][0]["task_path"] == "3.1"
        content = Path(nested_plan).read_text(encoding="utf-8")
        assert "## Phase 3" in content

    async def test_atomic_rollback_on_transition_error(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """Illegal transition rolls back all changes atomically."""
        # 1.2 [ ] -> [x] (valid) then 1.1.1.1 [ ] -> [x!] (valid) — then a failed one
        updates = [
            {"task_path": "1.1.2", "new_status": "[—]"},  # valid [ ] -> [—]
            {"task_path": "1.2", "new_status": "[x]"},  # valid
            {"task_path": "1.1", "new_status": "[x!]"},  # valid
            {"task_path": "1.1.2", "new_status": "[x]"},  # ILLEGAL: [—] is terminal
        ]
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=updates,
        )
        assert result["success"] is False
        assert result["rolled_back"] is True
        assert result["successful"] == []
        assert result["failed"][0]["task_path"] == "1.1.2"
        assert "valid_targets" in result["failed"][0]
        # Nothing persisted — full rollback
        check = await read_plan_tasks(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
        t1 = check["phases"][0]["tasks"][0]
        assert t1["subtasks"][1]["status"] == "[ ]"  # 1.1.2 back to [ ]
        assert t1["status"] == "[ ]"  # 1.1 back to [ ]

    async def test_transition_error_returns_valid_targets(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """Illegal transition surfaces valid_targets for the agent."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[{"task_path": "1.1", "new_status": "[—]"}, {"task_path": "1.1", "new_status": "[x]"}],
        )
        # Second update on 1.1: [—] is terminal -> [x] illegal
        assert result["success"] is False
        failed = result["failed"][0]
        assert failed["task_path"] == "1.1"
        assert failed["old_status"] == "[—]"
        assert failed["valid_targets"] == ["(none)"]

    async def test_idempotent_skip(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """Already-in-target-state updates are skipped, not executed."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[{"task_path": "1.1", "new_status": "[ ]"}, {"task_path": "1.2", "new_status": "[ ]"}],
        )
        assert result["success"] is True
        assert result["executed"] == []
        assert len(result["skipped"]) == 2
        assert result["successful"] == []

    async def test_missing_task_without_description_fails(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """A missing task without a description is an error, not auto-created."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[{"task_path": "2.99", "new_status": "[x]"}],
        )
        assert result["success"] is False
        assert result["failed"][0]["error"] == "Task not found"

    async def test_batch_trace_fields_present(
        self, temp_project_dir, project_id, plan_uuid, nested_plan, mock_agent_recall_success
    ):
        """Batch response exposes executed / skipped / created / successful / failed."""
        result = await batch_update_tasks(
            workspace_path=temp_project_dir,
            project_id=project_id,
            plan_uuid=plan_uuid,
            updates=[
                {"task_path": "1.1", "new_status": "[x]"},
                {"task_path": "2.2", "new_status": "[x]", "description": "New task"},
            ],
        )
        assert result["success"] is True
        assert result["executed"] == [{"task_path": "1.1", "old_status": "[ ]", "new_status": "[x]"}]
        assert len(result["created"]) == 1
        assert len(result["successful"]) == 2
        assert result["failed"] == []
        assert result["skipped"] == []
