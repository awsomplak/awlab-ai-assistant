"""Investigation: task_update batch anomaly (live-server report, 2026-09-09).

The live MCP (plan ef279450) reported "Task not found" for the LAST task of a
phase when a whole phase was marked in one batch (e.g. [1.1,1.2,1.3] failed on
1.3; [2.1,2.2,2.3,2.4] failed on 2.4), and some single updates claimed success
without the marker landing. The source parser resolved every path in isolation,
so these tests recreate the EXACT tasks.md shape (flat per-phase tasks with
``→ depends:`` continuation lines and ``[/]`` in-progress markers) and drive the
real ``batch_update_tasks`` through the reported batches. Green = source is
correct and the anomaly is live-server/environmental (rebuild+republish to
clear); red = source bug reproduced.
"""

from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.tools.plan_tools import batch_update_tasks

REAL_SHAPE_MD = """# Tasks

## Phase 1: Diagnose & reproduce the orphan worker leak (cross-platform)
- [/] Task 1: Reproduce the leak locally
- [ ] Task 2: Instrument bridge + worker lifecycle logging
    → depends: 1.1
- [ ] Task 3: Confirm the cross-platform claim
    → depends: 1.1

## Phase 2: Orphan self-termination — parent-death watchdog in the worker
- [/] Task 1: Bridge writes its PID to the worker env
    → depends: 1.3
- [ ] Task 2: Add a stdlib/ctypes-only parent-liveness watchdog
    → depends: 2.1
- [ ] Task 3: On parent death the watchdog triggers a clean shutdown
    → depends: 2.2
- [ ] Task 4: Unit-test the watchdog with a stub parent
    → depends: 2.3
"""


@pytest.fixture
def real_shape_plan(temp_project_dir: Path, plan_dir: Path, plan_uuid: str) -> str:
    tasks = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid)
    tasks.write_text(REAL_SHAPE_MD, encoding="utf-8")
    return str(tasks)


async def _status_of(content: str, task_path: str) -> str | None:
    from mcp_server.helpers import get_task_status

    return get_task_status(content, task_path)


async def test_batch_marks_entire_phase1(
    temp_project_dir, project_id, plan_uuid, real_shape_plan, mock_agent_recall_success
):
    """Reported failing batch A: [1.1, 1.2, 1.3] -> [x] (1.1 starts [/])."""
    result = await batch_update_tasks(
        workspace_path=temp_project_dir,
        project_id=project_id,
        plan_uuid=plan_uuid,
        updates=[
            {"task_path": "1.1", "new_status": "[x]"},
            {"task_path": "1.2", "new_status": "[x]"},
            {"task_path": "1.3", "new_status": "[x]"},
        ],
    )
    assert result["success"] is True, result
    assert result["failed"] == []
    content = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid).read_text(
        encoding="utf-8"
    )
    for path in ("1.1", "1.2", "1.3"):
        assert await _status_of(content, path) == "[x]", f"{path} not landed"


async def test_batch_marks_entire_phase2(
    temp_project_dir, project_id, plan_uuid, real_shape_plan, mock_agent_recall_success
):
    """Reported failing batch B: [2.1, 2.2, 2.3, 2.4] -> [x] (2.1 starts [/])."""
    result = await batch_update_tasks(
        workspace_path=temp_project_dir,
        project_id=project_id,
        plan_uuid=plan_uuid,
        updates=[
            {"task_path": "2.1", "new_status": "[x]"},
            {"task_path": "2.2", "new_status": "[x]"},
            {"task_path": "2.3", "new_status": "[x]"},
            {"task_path": "2.4", "new_status": "[x]"},
        ],
    )
    assert result["success"] is True, result
    assert result["failed"] == []
    content = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid).read_text(
        encoding="utf-8"
    )
    for path in ("2.1", "2.2", "2.3", "2.4"):
        assert await _status_of(content, path) == "[x]", f"{path} not landed"


async def test_single_update_lands_on_disk(
    temp_project_dir, project_id, plan_uuid, real_shape_plan, mock_agent_recall_success
):
    """Reported: single update reported success but marker did not land."""
    result = await batch_update_tasks(
        workspace_path=temp_project_dir,
        project_id=project_id,
        plan_uuid=plan_uuid,
        updates=[{"task_path": "2.1", "new_status": "[x]"}],
    )
    assert result["success"] is True, result
    content = settings.get_plan_tasks_path(workspace_path=temp_project_dir, plan_uuid=plan_uuid).read_text(
        encoding="utf-8"
    )
    assert await _status_of(content, "2.1") == "[x]", "single update did not land on disk"


def test_in_progress_and_skipped_markers_are_parseable():
    """[/] (in-progress) and [-] (skipped) tasks must resolve to their real paths.

    Regression: _TASK_RE omitted '/' and '-' so a `[/]` first task was invisible,
    shifting every later path in the phase down by one — the real last task then
    resolved as "Task not found" in batch updates.
    """
    from mcp_server.helpers import get_task_status, parse_tasks_md

    MD = """# Tasks

## Phase 1: Demo
- [/] Task 1: in progress
- [ ] Task 2: pending
- [-] Task 3: skipped
- [x] Task 4: done
"""
    parsed = parse_tasks_md(MD)
    assert [t["path"] for t in parsed["phases"][0]["tasks"]] == ["1.1", "1.2", "1.3", "1.4"]
    assert get_task_status(MD, "1.1") == "[/]"
    assert get_task_status(MD, "1.3") == "[-]"
    assert get_task_status(MD, "1.4") == "[x]"
