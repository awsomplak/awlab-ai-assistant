"""
Plan-level tools — registry management, workflow execution, retrospective
generation, and dependency resolution.

Extracted from the original monolithic plan_tools.py.
"""

import re
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    agent_recall,
    compute_tasks_summary,
    fail_obj,
    get_tasks_in_phase,
    parse_tasks_md,
    read_utf8,
    resolve_dep_status,
    validate_uuid,
    validate_workspace_path,
)
from ...helpers.registry_utils import (
    parse_registry,
)
from ...helpers.registry_utils import (
    switch_active_plan as _switch_active_plan,
)
from .io import (
    _pattern_name,
    store_memory_checkpoint,
    store_pattern_entity,
    sync_to_agent_recall,
    update_registry_phase_count,
)

# ── Registry Tools ─────────────────────────────────────────────────────────


async def list_registry(workspace_path: str, project_id: str | None = None) -> dict[str, Any]:
    """Return the three registry tables as JSON."""
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    try:
        return parse_registry(workspace_path)
    except Exception as e:
        return fail_obj(error=f"Failed to parse registry: {e}")


async def switch_active_plan(
    workspace_path: str | Path,
    project_id: str | None = None,
    uuid: str = "",
) -> dict[str, Any]:
    """Change the active plan in the registry."""
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(uuid):
        return fail_obj(error="Invalid UUID format.")
    return _switch_active_plan(workspace_path, uuid)


# ── Phase Completion Tools ────────────────────────────────────────────────


def _validate_phase_inputs(plan_uuid: str, phase_num: int) -> tuple[bool, dict[str, Any] | None]:
    """Validate plan_uuid and phase_num. Returns (is_valid, error_dict_or_None)."""
    if not validate_uuid(plan_uuid):
        return False, {"success": False, "error": "Invalid plan_uuid format."}
    if phase_num < 1:
        return False, {"success": False, "error": f"Invalid phase_number: {phase_num}. Phase number must be >= 1."}
    return True, None


def _read_phase_tasks(
    workspace_path: str | Path,
    plan_uuid: str,
    phase_num: int,
) -> tuple[str | None, list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Read tasks.md and return (content, tasks_in_phase, error_dict)."""
    tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)
    content = read_utf8(tasks_path)
    if content is None:
        return None, None, {"success": False, "error": f"tasks.md not found for plan '{plan_uuid}'"}

    tasks_in_phase = get_tasks_in_phase(content, phase_num)
    if tasks_in_phase is None or len(tasks_in_phase) == 0:
        return None, None, {"success": False, "error": f"Phase {phase_num} not found in plan '{plan_uuid}'"}

    return content, tasks_in_phase, None


def _check_non_terminal_tasks(tasks_in_phase: list[dict[str, Any]], phase_num: int) -> dict[str, Any] | None:
    """Check for non-terminal tasks. Returns error dict if found, None if all terminal."""
    non_terminal = [t for t in tasks_in_phase if t["status"] not in ("[x]", "[x✓]", "[x!]", "[—]")]
    if non_terminal:
        descriptions = [f"'{t['description']}' ({t['status']})" for t in non_terminal]
        return {
            "success": False,
            "error": f"Phase {phase_num} has {len(non_terminal)} non-terminal task(s): {', '.join(descriptions)}",
            "completed_count": sum(1 for t in tasks_in_phase if t["status"] in ("[x]", "[x✓]", "[x!]", "[—]")),
            "tasks_total": len(tasks_in_phase),
            "non_terminal": non_terminal,
        }
    return None


def _build_task_updates(content: str, phase_num: int) -> list[dict[str, str]]:
    """Build a list of {task_path, new_status} for all top-level tasks in a phase."""
    updates: list[dict[str, str]] = []
    phase_parsed = parse_tasks_md(content)
    for phase in phase_parsed["phases"]:
        if phase["phase_number"] == phase_num:
            top_tasks = [t for t in phase["tasks"] if t["indent"] == 0]
            for i, task in enumerate(top_tasks):
                task_path = f"{phase_num}.{i + 1}"
                updates.append({"task_path": task_path, "new_status": task["status"]})
            break
    return updates


def _find_next_phase(content: str, phase_num: int) -> int | None:
    """Find the next phase number after the given phase."""
    all_parsed = parse_tasks_md(content)
    for p in all_parsed["phases"]:
        if p["phase_number"] > phase_num:
            return p["phase_number"]
    return None


def _build_phase_task_list(tasks_in_phase: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build a simplified task list for the result."""
    return [{"description": t["description"], "status": t["status"]} for t in tasks_in_phase]


async def mark_phase_complete(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    phase_num: int = 0,
) -> dict[str, Any]:
    """
    Mark all tasks in a given phase as completed and update registry/synced state.

    Implements an 8-step workflow:
    1. Fresh-read tasks.md from disk
    2. Cross-check — verify ALL tasks in phase are terminal ([x]/[—])
    3-8. Sync, registry update, memory checkpoint, verify, return

    Args:
        workspace_path: the workspace (project root) path.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        phase_num: Phase number to mark complete.

    Returns:
        {success, completed_count, next_phase, registry_updated, memory_stored, ...}
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)

    # Steps 0-1: Validate inputs + fresh-read
    valid, error = _validate_phase_inputs(plan_uuid, phase_num)
    if not valid:
        return error  # type: ignore[return-value]

    errors: list[str] = []
    tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)

    content, tasks_in_phase, error = _read_phase_tasks(workspace_path, plan_uuid, phase_num)
    if error:
        return error
    assert tasks_in_phase is not None
    assert content is not None

    # Step 2: Cross-check terminal status
    non_terminal_error = _check_non_terminal_tasks(tasks_in_phase, phase_num)
    if non_terminal_error:
        return non_terminal_error

    # Step 3: Build updates list
    updates = _build_task_updates(content, phase_num)

    # Step 4: Sync changes to agent-recall DB
    db_synced = sync_to_agent_recall(
        workspace_path=workspace_path, plan_uuid=plan_uuid, updates=updates, project_id=project_id
    )
    if not db_synced:
        errors.append("Failed to sync to agent-recall DB (queued for retry)")

    # Step 5: Update registry.md counts
    registry_updated = update_registry_phase_count(workspace_path, plan_uuid)
    if not registry_updated:
        errors.append("Failed to update registry.md")

    # Step 6: Store memory checkpoint
    assert tasks_in_phase is not None
    memory_message = f"Phase {phase_num} completed. All {len(tasks_in_phase)} tasks terminal."
    memory_result = store_memory_checkpoint(
        workspace_path=workspace_path,
        plan_uuid=plan_uuid,
        phase_num=phase_num,
        message=memory_message,
        project_id=project_id,
    )
    memory_stored = memory_result.get("success", False)
    if not memory_stored:
        errors.append(f"Failed to store memory checkpoint: {memory_result.get('error', 'unknown')}")

    # Step 7: Verify file write succeeded
    verify_content = read_utf8(tasks_path)
    file_ok = verify_content is not None
    if not file_ok:
        errors.append("Failed to verify file write")

    # Determine next phase + build result lists
    next_phase = _find_next_phase(content, phase_num)
    phase_tasks_list = _build_phase_task_list(tasks_in_phase)

    # Step 8: Return structured result
    result: dict[str, Any] = {
        "success": len(errors) == 0,
        "completed_count": len(tasks_in_phase),
        "next_phase": next_phase,
        "registry_updated": registry_updated,
        "memory_stored": memory_stored,
        "notes_summary": f"Phase {phase_num} completed",
        "phase_tasks": phase_tasks_list,
    }
    if errors:
        result["errors"] = errors
        result["success"] = False

    return result


# ── Dependency Resolution ─────────────────────────────────────────────────


def _evaluate_task_dependencies(
    task: dict[str, Any],
    task_status_map: dict[str, str],
    tasks_in_phase: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Check a single task's dependencies against a status lookup.

    Dependency refs are resolved path-first (``1.2``) then by description
    fragment (legacy), via ``resolve_dep_status``. When a task list is provided
    the path resolution is exact; otherwise falls back to the description map.

    Returns:
        (deps_met, deps_unmet) — lists of dependency names.
    """
    deps_met: list[str] = []
    deps_unmet: list[str] = []
    tasks = tasks_in_phase or []
    for dep_name in task.get("dependencies", []):
        status = resolve_dep_status(dep_name, tasks) if tasks else None
        if status is None:
            # Fall back to the legacy description-substring map.
            dep_lower = dep_name.lower()
            found = False
            for desc, st in task_status_map.items():
                if dep_lower in desc:
                    found = True
                    status = st
                    break
            if not found:
                deps_unmet.append(f"'{dep_name}' not found")
                continue
        if status in ("[x]", "[x✓]", "[x!]", "[—]"):
            deps_met.append(dep_name)
        else:
            deps_unmet.append(f"'{dep_name}' is '{status}'")
    return deps_met, deps_unmet


def _resolve_task_path(
    parsed: dict[str, Any],
    pnum: int,
    description: str,
) -> str | None:
    """Resolve a task description to its task_path (e.g. '3.1')."""
    for phase in parsed["phases"]:
        if phase["phase_number"] == pnum:
            top_tasks = [t for t in phase["tasks"] if t["indent"] == 0]
            for i, t in enumerate(top_tasks):
                if t["description"] == description:
                    return f"{pnum}.{i + 1}"
            break
    return None


def _get_phases_to_scan(parsed: dict[str, Any], phase_number: int | None) -> list[int]:
    """Determine which phase numbers to scan."""
    if phase_number is not None:
        return [phase_number]
    return [p["phase_number"] for p in parsed["phases"]]


async def resolve_deferred_tasks(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    phase_number: int | None = None,
) -> dict[str, Any]:
    """
    Re-evaluate deferred ([⏳]) tasks in a plan and determine which are now eligible.

    Args:
        workspace_path: the workspace (project root) path.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        phase_number: Optional phase number to scan (if None, scans all phases).

    Returns:
        {success, resolved, remaining, total_deferred, cascade_failure}
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)
    content = read_utf8(tasks_path)
    if content is None:
        return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}'")

    parsed = parse_tasks_md(content)
    phases_to_scan = _get_phases_to_scan(parsed, phase_number)

    all_now_eligible: list[dict[str, Any]] = []
    all_still_deferred: list[dict[str, Any]] = []
    total_deferred = 0

    for pnum in phases_to_scan:
        tasks_in_phase = get_tasks_in_phase(content, pnum)
        if tasks_in_phase is None:
            continue

        deferred_tasks = [t for t in tasks_in_phase if t["status"] == "[⏳]"]
        if not deferred_tasks:
            continue

        total_deferred += len(deferred_tasks)

        # Build lookup: description.lower() → status
        task_status_map: dict[str, str] = {t["description"].lower(): t["status"] for t in tasks_in_phase}

        for task in deferred_tasks:
            deps_met, deps_unmet = _evaluate_task_dependencies(task, task_status_map, tasks_in_phase=tasks_in_phase)

            task_path = _resolve_task_path(parsed, pnum, task["description"])
            entry: dict[str, Any] = {
                "task_path": task_path,
                "description": task["description"],
            }

            if deps_unmet:
                entry["reasons"] = deps_unmet
                all_still_deferred.append(entry)
            else:
                entry["dependencies_met"] = deps_met
                all_now_eligible.append(entry)

    cascade_failure = len(all_now_eligible) == 0 and len(all_still_deferred) > 0

    return {
        "success": True,
        "resolved": all_now_eligible,
        "remaining": all_still_deferred,
        "total_deferred": total_deferred,
        "cascade_failure": cascade_failure,
    }


# ── Plan Completable Check ────────────────────────────────────────────────


def _scan_incomplete_tasks(content: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Scan all phases for non-terminal tasks. Returns a list of incomplete task dicts."""
    incomplete: list[dict[str, Any]] = []
    for phase in parsed["phases"]:
        tasks_in_phase = get_tasks_in_phase(content, phase["phase_number"])
        if not tasks_in_phase:
            continue
        for task in tasks_in_phase:
            if task["status"] not in ("[x]", "[x✓]", "[x!]", "[—]"):
                incomplete.append(
                    {
                        "phase_number": phase["phase_number"],
                        "name": phase["name"],
                        "description": task["description"],
                        "status": task["status"],
                    }
                )
    return incomplete


def _count_x_warnings(content: str, parsed: dict[str, Any]) -> int:
    """Count all tasks in [x!] (completed with warnings) status."""
    count = 0
    for phase in parsed["phases"]:
        tasks_in_phase = get_tasks_in_phase(content, phase["phase_number"])
        if tasks_in_phase:
            for t in tasks_in_phase:
                if t["status"] == "[x!]":
                    count += 1
    return count


def _build_noncompletable_reasons(incomplete_tasks: list[dict[str, Any]]) -> list[str]:
    """Build human-readable reasons from the incomplete tasks list."""
    deferred = [t for t in incomplete_tasks if t["status"] == "[⏳]"]
    failed = [t for t in incomplete_tasks if t["status"] == "[!]"]
    pending = [t for t in incomplete_tasks if t["status"] == "[ ]"]
    reasons: list[str] = []
    if pending:
        reasons.append(f"{len(pending)} pending task(s) found")
    if deferred:
        reasons.append(f"{len(deferred)} deferred task(s) found")
    if failed:
        reasons.append(f"{len(failed)} failed task(s) found")
    return reasons


async def check_plan_completable(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
) -> dict[str, Any]:
    """
    Check whether ALL tasks across ALL phases are in a terminal state.

    Args:
        workspace_path: the workspace (project root) path.
        plan_uuid: 8-character lowercase alphanumeric UUID.

    Returns:
        {success, completable, incomplete_tasks, summary, reasons}
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)
    content = read_utf8(tasks_path)

    if content is None:
        return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}'")

    parsed = parse_tasks_md(content)
    incomplete_tasks = _scan_incomplete_tasks(content, parsed)
    completable = len(incomplete_tasks) == 0

    # Compute summary — count [x!] as completed
    summary = compute_tasks_summary(content)
    summary["completed"] = summary.get("completed", 0) + _count_x_warnings(content, parsed)

    result: dict[str, Any] = {
        "success": True,
        "completable": completable,
        "incomplete_tasks": incomplete_tasks,
        "summary": summary,
    }

    if not completable:
        result["reasons"] = _build_noncompletable_reasons(incomplete_tasks)

    return result


# ── Next Eligible Task ────────────────────────────────────────────────────


async def get_next_eligible_task(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    phase: int | None = None,
) -> dict[str, Any]:
    """
    Find the next eligible (non-blocked, non-terminal) task in a plan.

    Uses dependency annotations: if a task has \u2192 depends: and its
    dependency is unmet, marks the task as deferred.

    Args:
        workspace_path: the workspace (project root) path.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        phase: Optional phase number to limit the search to a specific phase. If None, scans all phases in order.
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)
    content = read_utf8(tasks_path)

    if content is None:
        return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}'")

    if phase is not None:
        result = _get_next_eligible_task_local(content, phase)
        result["success"] = True
        return result

    # Scan phases in order
    parsed = parse_tasks_md(content)
    for p in parsed["phases"]:
        result = _get_next_eligible_task_local(content, p["phase_number"])
        if result["next_task"] is not None:
            result["phase_number"] = p["phase_number"]
            result["success"] = True
            return result
        if result["cascade_failure"]:
            result["phase_number"] = p["phase_number"]
            result["success"] = True
            continue

    return {
        "success": True,
        "next_task": None,
        "deferred": [],
        "completed": [],
        "all_terminal": True,
        "cascade_failure": False,
    }


def _get_next_eligible_task_local(content: str, phase_num: int) -> dict[str, Any]:
    """
    Find the next eligible task within a specific phase.

    Returns: {next_task, deferred, completed, all_terminal, cascade_failure}
    """
    tasks_in_phase = get_tasks_in_phase(content, phase_num)
    if tasks_in_phase is None:
        return {
            "next_task": None,
            "deferred": [],
            "completed": [],
            "all_terminal": True,
            "cascade_failure": False,
        }

    deferred_tasks = [t for t in tasks_in_phase if t["status"] == "[⏳]"]
    completed_tasks = [t for t in tasks_in_phase if t["status"] in ("[x]", "[x✓]", "[x!]", "[—]")]
    failed_tasks = [t for t in tasks_in_phase if t["status"] == "[!]"]

    # Build a status map for dependency checking (fallback path)
    task_status_map: dict[str, str] = {}
    for t in tasks_in_phase:
        task_status_map[t["description"].lower()] = t["status"]

    # Filter eligible tasks — those with [ ] status AND whose dependencies are met
    all_pending = [t for t in tasks_in_phase if t["status"] == "[ ]"]
    eligible_tasks = []
    blocked_pending = []
    for task in all_pending:
        deps_met, _ = _evaluate_task_dependencies(task, task_status_map, tasks_in_phase=tasks_in_phase)
        if len(deps_met) == len(task.get("dependencies", [])):
            eligible_tasks.append(task)
        else:
            blocked_pending.append(task)

    # Check for cascade failure:
    # - All pending tasks are blocked by unmet dependencies
    # - AND there are deferred tasks (also blocked)
    all_pending_blocked = len(all_pending) > 0 and len(eligible_tasks) == 0
    cascade_failure = all_pending_blocked and len(deferred_tasks) > 0

    # all_terminal: no pending or failed tasks remain
    all_terminal = len(all_pending) == 0 and len(failed_tasks) == 0

    next_task = eligible_tasks[0] if eligible_tasks else None

    return {
        "next_task": next_task,
        "deferred": deferred_tasks,
        "completed": completed_tasks,
        "all_terminal": all_terminal,
        "cascade_failure": cascade_failure,
    }


# ── Workflow Execution ─────────────────────────────────────────────────────


def _execute_workflow_step(step: dict[str, Any]) -> dict[str, Any]:
    """
    Execute a single workflow step and return its result entry.

    Returns a dict with: step_name, type, status, output.
    """
    step_name = step.get("name", "unnamed")
    step_type = step.get("type", "log")

    try:
        if step_type == "agent_task":
            return {
                "step_name": step_name,
                "type": "agent_task",
                "status": "pending",
                "output": step.get("description", ""),
            }
        elif step_type == "mcp_tool":
            return {
                "step_name": step_name,
                "type": "mcp_tool",
                "status": "pending",
                "output": f"Requires MCP tool call: {step.get('tool', 'unknown')}",
            }
        elif step_type == "file_op":
            file_path = step.get("path", "")
            op = step.get("operation", "read")
            return {
                "step_name": step_name,
                "type": "file_op",
                "status": "validated",
                "output": f"File {op}: {file_path}",
            }
        else:
            return {
                "step_name": step_name,
                "type": step_type,
                "status": "passed",
                "output": step.get("description", f"Executed step: {step_name}"),
            }
    except Exception as e:
        return {
            "step_name": step_name,
            "type": step_type,
            "status": "error",
            "output": str(e),
        }


def _load_workflow(
    workflow_name: str,
    workflows_dir: str | Path | None = None,
    workspace_path: str | Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Load and parse a workflow file from the shared workflows directory.

    Args:
        workflow_name: Name of the workflow file (with or without .md extension).
        workflows_dir: Optional override for the workflows directory.
                       Defaults to ~/.awlab-id/agent-memory/work-flows.
        workspace_path: Unused (kept for signature compatibility); workflows are
                        workspace-independent step definitions.

    Returns:
        (workflow_def, error_string_or_None)
    """
    if not workflow_name:
        return None, "workflow_name is required"

    # Normalize name
    if not workflow_name.endswith(".md"):
        workflow_name += ".md"

    if workflows_dir is None:
        workflows_dir = settings.workflows_dir
    workflows_dir = Path(workflows_dir)
    workflow_path = workflows_dir / workflow_name

    if not workflow_path.exists():
        return None, f"Workflow '{workflow_name}' not found in {workflows_dir.name}/"

    content = read_utf8(workflow_path)
    if content is None:
        return None, f"Failed to read workflow '{workflow_name}'"

    return _parse_workflow_md(content, workflow_name), None


async def execute_workflow(
    workspace_path: str | Path | None = None,
    project_id: str | None = None,
    workflow_name: str = "",
    params: dict[str, Any] | None = None,
    workflows_dir: str | Path | None = None,
) -> dict[str, Any]:
    """
    Execute a workflow by loading its definition from the shared workflows dir
    and running the defined steps.

    Args:
        workspace_path: optional (workspace-independent); validated only when provided.
        workflow_name: Name of the workflow file (with or without .md extension).
        params: Optional parameters for the workflow.
        workflows_dir: Optional override for the workflows directory.
                       Defaults to ~/.awlab-id/agent-memory/work-flows.

    Returns:
        {success, workflow, steps, error}
    """
    # Validate workspace_path only when provided (workflows are workspace-free)
    if workspace_path:
        valid, err = validate_workspace_path(workspace_path)
        if not valid:
            return fail_obj(error=err)
    workflow_def, error = _load_workflow(workflow_name, workflows_dir=workflows_dir)
    if error:
        return fail_obj(error=error)

    # Execute steps
    steps: list[dict[str, Any]] = []
    overall_error: str | None = None
    params = params or {}

    assert workflow_def is not None
    for step in workflow_def.get("steps", []):
        step_result = _execute_workflow_step(step)
        steps.append(step_result)
        if step_result["status"] == "error" and overall_error is None:
            overall_error = f"Step '{step_result['step_name']}' failed: {step_result['output']}"

    result: dict[str, Any] = {
        "success": overall_error is None,
        "workflow": workflow_name.replace(".md", "") if ".md" in (workflow_name or "") else workflow_name,
        "steps": steps,
    }
    if overall_error:
        result["error"] = overall_error

    return result


def _parse_workflow_md(content: str, filename: str) -> dict[str, Any]:
    """
    Parse a workflow markdown file into a structured definition.

    Returns:
        {name, description, steps}
    """
    name = filename.replace(".md", "")
    description = ""
    steps: list[dict[str, Any]] = []

    lines = content.splitlines()
    current_step: dict[str, Any] | None = None

    for line in lines:
        stripped = line.strip()

        # YAML frontmatter
        if stripped == "---" and current_step is None:
            continue

        # Title / name
        if stripped.startswith("# ") and current_step is None:
            name = stripped[2:].strip()
            continue

        # Description
        if stripped.startswith("description:"):
            description = stripped[12:].strip().strip(">").strip()
            if description == "" or description == ">":
                description = ""
            continue

        # Headers within workflow (steps)
        if stripped.startswith("## "):
            if current_step:
                steps.append(current_step)
            current_step = {
                "name": stripped[3:].strip(),
                "type": "log",
                "description": "",
            }
            continue

        # Action markers
        if current_step is not None:
            if stripped.startswith("- `"):
                type_match = re.match(r"- `(\w+)`\s*(.*)", stripped)
                if type_match:
                    current_step["type"] = type_match.group(1)
                    current_step["description"] = type_match.group(2).strip()
                else:
                    current_step["description"] = stripped.lstrip("- ")
            elif stripped.startswith("-"):
                current_step["description"] = stripped.lstrip("- ")

            # Specific extraction for known patterns
            if "tool:" in stripped.lower() or "use_mcp_tool" in stripped:
                current_step["type"] = "mcp_tool"
                tool_match = re.search(r'tool_name["\s:]+(\w+)', stripped)
                if tool_match:
                    current_step["tool"] = tool_match.group(1)

            if "file:" in stripped.lower() or "path:" in stripped.lower():
                current_step["type"] = "file_op"
                path_match = re.search(r'["\']([^"\']+\.\w+)["\']', stripped)
                if path_match:
                    current_step["path"] = path_match.group(1)

            if "agent" in stripped.lower() or "respond" in stripped.lower():
                current_step["type"] = "agent_task"

    # Append last step
    if current_step:
        steps.append(current_step)

    return {
        "name": name,
        "description": description,
        "steps": steps,
    }


async def list_workflows(
    workspace_path: str | Path | None = None,
    project_id: str | None = None,
    workflows_dir: str | Path | None = None,
) -> dict[str, Any]:
    """
    List all available workflow files in the shared workflows directory.

    Args:
        workspace_path: optional (workspace-independent); validated only when provided.
        workflows_dir: Optional override for the workflows directory.
                       Defaults to ~/.awlab-id/agent-memory/work-flows.

    Returns:
        {success, workflows, count}
    """
    # Validate workspace_path only when provided (workflows are workspace-free)
    if workspace_path:
        valid, err = validate_workspace_path(workspace_path)
        if not valid:
            return fail_obj(error=err)
    if workflows_dir is None:
        workflows_dir = settings.workflows_dir
    workflows_dir = Path(workflows_dir)

    if not workflows_dir.exists():
        return {
            "success": False,
            "error": f"Workflows directory not found at {workflows_dir}",
        }

    md_files = sorted(workflows_dir.glob("*.md"))
    workflows: list[dict[str, Any]] = []

    for md_file in md_files:
        content = read_utf8(md_file)
        if content is None:
            continue

        parsed = _parse_workflow_md(content, md_file.name)

        workflows.append(
            {
                "name": md_file.name.replace(".md", ""),
                "description": parsed.get("description", ""),
                "steps_count": len(parsed.get("steps", [])),
                "file": md_file.name,
            }
        )

    return {
        "success": True,
        "workflows": workflows,
        "count": len(workflows),
    }


# ── Retrospective ─────────────────────────────────────────────────────────


def _extract_plan_name(plan_content: str | None) -> str:
    """Extract the plan name from plan.md content."""
    if plan_content:
        for line in plan_content.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return ""


def _compute_plan_summary(tasks_content: str) -> dict[str, Any]:
    """Compute completion statistics from tasks.md content."""
    parsed = parse_tasks_md(tasks_content)
    top_level_tasks = []
    for phase in parsed["phases"]:
        tasks_in_phase = get_tasks_in_phase(tasks_content, phase["phase_number"])
        if tasks_in_phase:
            for t in tasks_in_phase:
                if t.get("indent", 0) == 0:
                    top_level_tasks.append(t)

    return {
        "total_tasks": len(top_level_tasks),
        "completed": sum(1 for t in top_level_tasks if t["status"] in ("[x]", "[x✓]", "[x!]")),
        "skipped": sum(1 for t in top_level_tasks if t["status"] == "[—]"),
        "failed": sum(1 for t in top_level_tasks if t["status"] == "[!]"),
    }


def _extract_patterns_from_tasks(
    tasks_content: str,
    plan_uuid: str,
) -> list[dict[str, Any]]:
    """
    Extract successful patterns from completed tasks.

    Each completed top-level task becomes a candidate pattern.
    """
    parsed = parse_tasks_md(tasks_content)
    patterns: list[dict[str, Any]] = []

    for phase in parsed["phases"]:
        tasks_in_phase = get_tasks_in_phase(tasks_content, phase["phase_number"])
        if not tasks_in_phase:
            continue

        for task in tasks_in_phase:
            if task["status"] in ("[x]", "[x✓]") and task.get("indent", 0) == 0:
                desc = task["description"]
                clean_desc = re.sub(r"^Task\s+\d+:\s*", "", desc)
                if clean_desc:
                    pattern_name = _pattern_name("convention", clean_desc)
                    patterns.append(
                        {
                            "name": pattern_name,
                            "description": clean_desc,
                            "phase": phase["phase_number"],
                            "context": phase.get("name", phase.get("title", "")),
                            "source_plan": plan_uuid,
                        }
                    )
    return patterns


def _store_retro_patterns(
    workspace_path: str | Path,
    patterns: list[dict[str, Any]],
    plan_uuid: str,
    errors: list[str],
) -> tuple[list[str], int]:
    """
    Store extracted patterns as agent-recall entities and link them.

    Returns:
        (stored_names, count)
    """
    stored: list[str] = []
    count = 0
    for pattern in patterns[:5]:  # Limit to 5 patterns
        result = store_pattern_entity(
            workspace_path=workspace_path,
            name=pattern["name"],
            observation=f"Retrospective pattern: {pattern['description']}",
            pattern_type="convention",
            patterns=True,  # learned patterns live in the dedicated user-patterns store
            context=pattern.get("context", ""),
        )
        if result.get("success"):
            stored.append(pattern["name"])
            count += 1

    if stored:
        try:
            for pat_name in stored:
                agent_recall.create_relations(
                    workspace_path=workspace_path,
                    relations=[
                        {
                            "from": pat_name,
                            "to": f"plan_{plan_uuid}",
                            "relationType": "extracted_from",
                        }
                    ],
                    patterns=True,
                )
        except Exception as e:
            errors.append(f"Failed to link patterns: {e}")

    return stored, count


async def generate_retrospective_summary(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
) -> dict[str, Any]:
    """
    Generate a retrospective summary for a completed plan.

    Reads plan.md, tasks.md, notes.md, extracts patterns from completed tasks,
    stores them as entities in agent-recall, and returns the summary.

    Args:
        workspace_path: The workspace (project root) path.
        plan_uuid: 8-character lowercase alphanumeric UUID.

    Returns:
        {success, patterns_extracted, plan_summary, suggested_patterns, errors}
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    errors: list[str] = []
    plan_dir = settings.get_artifacts_dir(workspace_path) / plan_uuid

    # Step 1: Read plan.md, tasks.md
    plan_content = read_utf8(plan_dir / "plan.md")
    tasks_content = read_utf8(plan_dir / "tasks.md")

    if tasks_content is None:
        return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}'")

    # Extract plan name + compute summary
    plan_name = _extract_plan_name(plan_content)
    plan_summary = _compute_plan_summary(tasks_content)
    plan_summary["name"] = plan_name or plan_uuid

    # Step 2: Extract successful patterns from completed tasks
    suggested_patterns = _extract_patterns_from_tasks(tasks_content, plan_uuid)

    # Step 3-4: Store patterns + link them
    stored_patterns, patterns_extracted = _store_retro_patterns(
        workspace_path=workspace_path,
        patterns=suggested_patterns,
        plan_uuid=plan_uuid,
        errors=errors,
    )

    result: dict[str, Any] = {
        "success": patterns_extracted > 0,
        "plan_uuid": plan_uuid,
        "patterns_extracted": patterns_extracted,
        "plan_summary": plan_summary,
        "suggested_patterns": suggested_patterns[:10],
        "observations": {
            "patterns_extracted": patterns_extracted,
            "stored_patterns": stored_patterns,
        },
    }
    if errors:
        result["errors"] = errors

    return result
