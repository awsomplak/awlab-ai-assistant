"""
Tool registration for awlab-plan server — plan, registry, task, and workflow tools.

Creates its own FastMCP("awlab-plan") instance and registers only
plan/registry/task/workflow-related tools.
    """

import json
from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP
from ..helpers.logger import logger

# ── Own FastMCP instance ─────────────────────────────────────────────────

mcp = FastMCP("awlab-plan")


# ── Import business-logic implementations ─────────────────────────────────

from ..tools.plan_tools import (
    read_plan_tasks as _read_plan_tasks,
    update_task_status as _update_task_status,
    batch_update_tasks as _batch_update_tasks,
    write_plan_tasks as _write_plan_tasks,
    validate_phase_gate as _validate_phase_gate,
    get_next_eligible_task as _get_next_eligible_task,
    list_registry as _list_registry,
    switch_active_plan as _switch_active_plan,
    mark_phase_complete as _mark_phase_complete,
    resolve_deferred_tasks as _resolve_deferred_tasks,
    check_plan_completable as _check_plan_completable,
    execute_workflow as _execute_workflow,
    list_workflows as _list_workflows,
    generate_retrospective_summary as _generate_retrospective_summary,
)
from ..tools.utils_tools import (
    generate_mermaid as _generate_mermaid,
    format_tasks_as_markdown as _format_tasks_as_markdown,
)
from ..helpers import (
    VALID_STATUS_MARKERS,
    require_uuid as _require_uuid,
    require_status as _require_status,
    require_phase_number as _require_phase_number,
    invalid_format as _invalid_format,
    invalid_status as _invalid_status,
    invalid_status_marker as _invalid_status_marker,
    validate_status_transition as _validate_status_transition,
    ok_json as _ok,
    fail_json as _fail,
)


# ══════════════════════════════════════════════════════════════════════════
# ── Registry Tools ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="reg_list_registry")
async def list_registry(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")]) -> str:
    """
    List plans.
    Return Active, Paused, Completed plans as JSON."""
    result = await _list_registry(workspace_path=workspace_path)
    return json.dumps(result)


@mcp.tool(name="reg_switch_active_plan")
async def switch_active_plan(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    *,
    plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")]
) -> str:
    """
    Switch active plan.
    Change the active plan in the registry."""
    if err := _require_uuid(plan_uuid=plan_uuid):
        return err
    result = await _switch_active_plan(
        workspace_path=workspace_path,
        project_id=project_id,
        uuid=plan_uuid,
    )
    return json.dumps(result)


@mcp.tool(name="task_update_status")
async def update_task_status(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    *,
    plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")],
    task_path: Annotated[str, Field(description="Task like '1.2' (Phase 1, Task 2)")],
    new_status: Annotated[str, Field(description="Status: [x], [ ], [!], [x!], [x✓], [—], [⏳]")],
) -> str:
    """
    Update task status.
    Update a task's status marker in a plan."""
    if err := _require_uuid(plan_uuid=plan_uuid):
        return err
    if err := _require_status(status=new_status):
        return _invalid_status(status=new_status)
    result = await _update_task_status(
        workspace_path=workspace_path,
        project_id=project_id,
        plan_uuid=plan_uuid,
        task_path=task_path,
        new_status=new_status,
    )
    return json.dumps(result)


@mcp.tool(name="task_batch_update")
async def batch_update_tasks(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    *,
    plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")],
    updates: Annotated[list[dict[str, str]], Field(description="List of {task_path, new_status} dicts for batch update")],
) -> str:
    """
    Batch update tasks atomically.
    Atomically update multiple tasks with rollback on failure."""
    if err := _require_uuid(plan_uuid):
        return err
    if not isinstance(updates, list):
        return _fail("updates must be an array of {task_path, new_status} objects.")
    result = await _batch_update_tasks(
        workspace_path=workspace_path,
        project_id=project_id,
        plan_uuid=plan_uuid,
        updates=updates,
    )
    return json.dumps(result)


@mcp.tool(name="reg_validate_phase_gate")
async def validate_phase_gate(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    *,
    plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")],
    phase_num: Annotated[int, Field(description="Phase number to check")],
) -> str:
    """
    Check predecessor phase complete.
    Check if predecessor phase is complete before allowing next phase."""
    if err := _require_uuid(plan_uuid=plan_uuid):
        return err
    if err := _require_phase_number(phase_number=phase_num):
        return err
    result = await _validate_phase_gate(
        workspace_path=workspace_path,
        project_id=project_id,
        plan_uuid=plan_uuid,
        phase_num=phase_num,
    )
    return json.dumps(result)


@mcp.tool(name="reg_get_next_eligible_task")
async def get_next_eligible_task(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    *,
    plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")],
    phase: Annotated[int | None, Field(description="Phase number to scan (None = all)")] = None,
) -> str:
    """
    Find next non-blocked task.
    Find next eligible (non-blocked, non-terminal) task."""
    if err := _require_uuid(plan_uuid=plan_uuid):
        return err
    result = await _get_next_eligible_task(
        workspace_path=workspace_path,
        project_id=project_id,
        plan_uuid=plan_uuid,
        phase=phase,
    )
    return json.dumps(result)


@mcp.tool(name="task_validate_transition")
async def validate_status_transition(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    *,
    current: Annotated[str, Field(description="Current status marker")],
    target: Annotated[str, Field(description="Desired new status marker")],
) -> str:
    """
    Validate status transition.
    Check if a status marker transition is legal."""
    markers = set(VALID_STATUS_MARKERS)
    if current not in markers:
        return _invalid_status_marker(status=current, context="current")
    if target not in markers:
        return _invalid_status_marker(status=target, context="target")
    result = _validate_status_transition(current=current, target=target)
    return json.dumps(result)


@mcp.tool(name="task_read_plan_tasks")
async def read_plan_tasks(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")], format: Annotated[str, Field(description="Output format: structured, raw, or minimal")] = "structured") -> str:
    """
    Read tasks.md.
    Parse tasks.md into structured, raw, or minimal JSON."""
    if err := _require_uuid(plan_uuid):
        return err
    if format not in ("structured", "raw", "minimal"):
        return _invalid_format("format", format, ("structured", "raw", "minimal"))
    result = await _read_plan_tasks(workspace_path=workspace_path, plan_uuid=plan_uuid, format=format)
    return json.dumps(result)


@mcp.tool(name="task_write_plan_tasks")
async def write_plan_tasks(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")], content: Annotated[str, Field(description="Full markdown to write to tasks.md")]) -> str:
    """
    Write tasks.md.
    Write markdown content to a plan's tasks.md."""
    if err := _require_uuid(plan_uuid):
        return err
    result = await _write_plan_tasks(workspace_path=workspace_path, plan_uuid=plan_uuid, content=content)
    return json.dumps(result)


@mcp.tool(name="reg_mark_phase_complete")
async def mark_phase_complete(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")], phase_number: Annotated[int, Field(description="Phase number to mark complete")]) -> str:
    """
    Complete all tasks in phase.
    Mark all tasks in a phase as completed."""
    if err := _require_uuid(plan_uuid):
        return err
    if err := _require_phase_number(phase_number):
        return err
    result = await _mark_phase_complete(
        workspace_path=workspace_path,
        plan_uuid=plan_uuid,
        phase_num=phase_number,
    )
    return json.dumps(result)


@mcp.tool(name="reg_resolve_deferred_tasks")
async def resolve_deferred_tasks(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")], phase_number: Annotated[int | None, Field(description="Phase number to scan (None = all)")] = None) -> str:
    """
    Re-check deferred tasks.
    Re-evaluate deferred tasks whose deps may now be satisfied."""
    if err := _require_uuid(plan_uuid):
        return err
    result = await _resolve_deferred_tasks(
        plan_uuid=plan_uuid,
        phase_number=phase_number,
        workspace_path=workspace_path,
    )
    return json.dumps(result)


@mcp.tool(name="reg_check_plan_completable")
async def check_plan_completable(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")]) -> str:
    """
    Check if plan complete.
    Check if a plan can be marked as complete."""
    if err := _require_uuid(plan_uuid):
        return err
    result = await _check_plan_completable(
        plan_uuid=plan_uuid,
        workspace_path=workspace_path,
    )
    return json.dumps(result)


@mcp.tool(name="wf_execute")
async def execute_workflow(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], workflow_name: Annotated[str, Field(description="Workflow filename (without .md)")], params: Annotated[str | None, Field(description="Optional JSON string with workflow params")] = None) -> str:
    """
    Execute workflow.
    Execute a named workflow from Cline/Workflows/."""
    parsed_params = json.loads(params) if params else None
    result = await _execute_workflow(
        workspace_path=workspace_path,
        workflow_name=workflow_name,
        params=parsed_params,
    )
    return json.dumps(result)


@mcp.tool(name="wf_list")
async def list_workflows(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")]) -> str:
    """
    List workflows.
    List available workflow files in Cline/Workflows/."""
    result = await _list_workflows(workspace_path=workspace_path)
    return json.dumps(result)


@mcp.tool(name="reg_generate_retrospective")
async def generate_retrospective_summary(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], plan_uuid: Annotated[str, Field(description="8-char lowercase UUID of the plan")]) -> str:
    """
    Generate retrospective.
    Generate retrospective summary for a completed plan."""
    if err := _require_uuid(plan_uuid):
        return err
    result = await _generate_retrospective_summary(
        plan_uuid=plan_uuid,
        workspace_path=workspace_path,
    )
    return json.dumps(result)


# ══════════════════════════════════════════════════════════════════════════
# ── Utility Tools (plan-adjacent) ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="util_generate_mermaid")
async def generate_mermaid(phases: Annotated[list[str], Field(description="List of phase names")], dependencies: Annotated[list[dict[str, str]] | None, Field(description="List of {from, to} dependency edges")] = None) -> str:
    """Generate a Mermaid flowchart from phases and optional deps."""
    result = await _generate_mermaid(phases, dependencies)
    return json.dumps(result)


@mcp.tool(name="task_format_markdown")
async def format_tasks_as_markdown(
    workspace_path: Annotated[str | None, Field(description="Absolute path to the project workspace root")] = None,
    plan_uuid: Annotated[str | None, Field(description="8-char lowercase UUID of the plan")] = None,
    phases: Annotated[list[dict[str, str]] | None, Field(description="List of phase dicts with name and status")] = None,
) -> str:
    """
    Format tasks as markdown.
    Format a task list as markdown."""
    result = await _format_tasks_as_markdown(
        workspace_path,
        plan_uuid=plan_uuid,
        phases=phases,
    )
    return json.dumps(result)
