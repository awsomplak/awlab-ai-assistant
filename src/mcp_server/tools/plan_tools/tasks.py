"""
Task CRUD tools — read, update, batch-update, and parse tasks.md.

Extracted from the original monolithic plan_tools.py. Handles all
task-related operations including status validation and transitions.
"""

from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    VALID_STATUS_MARKERS,
    compute_tasks_summary,
    create_task_in_md,
    fail_obj,
    get_task_status,
    parse_tasks_md,
    read_utf8,
    update_task_status_in_md,
    validate_status,
    validate_status_transition,
    validate_uuid,
    validate_workspace_path,
    write_utf8,
)
from .io import append_pending, sync_to_agent_recall

# ── Internal Helpers ──────────────────────────────────────────────────────


def _extract_old_status(content: str, task_path: str) -> str | None:
    """Extract current status marker of a task from tasks.md content.

    Supports N-level dotted paths (``1.2`` / ``1.2.3``) via the shared
    indentation-based resolver in helpers.file_utils.
    """
    return get_task_status(content, task_path)


def _apply_task_mutation(
    content: str,
    task_path: str,
    new_status: str,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """
    Validate transition and apply a status mutation.

    Returns:
        (updated_content, old_status, error_dict_or_None)
    """
    old_status = _extract_old_status(content, task_path)
    if old_status is not None:
        transition_check = validate_status_transition(old_status, new_status)
        if not transition_check["valid"]:
            return (
                None,
                old_status,
                {
                    "success": False,
                    "error": transition_check["reason"],
                    "old_status": old_status,
                    "valid_targets": transition_check["valid_targets"],
                },
            )

    try:
        updated_content = update_task_status_in_md(content, task_path, new_status)
    except Exception as e:
        return None, old_status, {"success": False, "error": f"Failed to update task status: {e}"}

    if updated_content is None:
        return None, old_status, {"success": False, "error": f"Task '{task_path}' not found in plan"}
    return updated_content, old_status, None


def _write_with_pending_fallback(
    tasks_path: str,
    updated_content: str,
    workspace_path: str | Path,
    plan_uuid: str,
    task_path: str,
    new_status: str,
    old_status: str | None,
    pre_mutation_state: str,
) -> dict[str, Any]:
    """
    Write updated content to disk with a pending-queue fallback.

    Returns the final result dict for the update.
    """
    file_write_ok = write_utf8(path=tasks_path, content=updated_content)
    if not file_write_ok:
        pending_entry = {
            "type": "update_task_status",
            "plan_uuid": plan_uuid,
            "task_path": task_path,
            "new_status": new_status,
            "old_status": old_status,
            "pre_mutation_state": pre_mutation_state,
        }
        pending_ok = append_pending(workspace_path=workspace_path, entry=pending_entry)
        return {
            "success": False,
            "error": "Failed to write tasks.md. Operation queued to pending.jsonl.",
            "old_status": old_status,
            "file_path": str(tasks_path),
            "db_synced": False,
            "pre_mutation_state": pre_mutation_state,
            "pending_queued": pending_ok,
        }

    db_synced = sync_to_agent_recall(
        workspace_path=workspace_path,
        plan_uuid=plan_uuid,
        updates=[{"task_path": task_path, "new_status": new_status}],
    )
    return {
        "success": True,
        "old_status": old_status,
        "new_status": new_status,
        "file_path": str(tasks_path),
        "db_synced": db_synced,
        "pre_mutation_state": pre_mutation_state,
    }


def _build_failure_response(
    success: bool,
    successful: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    pre_mutation_state: str,
    rolled_back: bool = True,
) -> dict[str, Any]:
    """Build a standard batch-update failure response with rollback."""
    return {
        "success": success,
        "successful": successful,
        "failed": failed,
        "pre_mutation_state": pre_mutation_state,
        "rolled_back": rolled_back,
    }


# ── Read Tasks ────────────────────────────────────────────────────────────


async def read_plan_tasks(
    workspace_path: str | Path,
    plan_uuid: str,
    format: str = "structured",
) -> dict[str, Any]:
    """
    Read and parse a plan's tasks.md file.

    Args:
        workspace_path: Absolute path to the project workspace root.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        format: One of 'structured', 'raw', or 'minimal'.

    Returns:
        Dict with plan data.
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format. Must be 8 lowercase alphanumeric characters.")

    tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)
    content = read_utf8(path=tasks_path)
    if content is None:
        return {
            "success": False,
            "error": f"tasks.md not found for plan '{plan_uuid}'",
        }

    if format == "raw":
        return {"success": True, "plan_uuid": plan_uuid, "format": "raw", "content": content}

    if format == "minimal":
        try:
            summary = compute_tasks_summary(content)
            return {
                "success": True,
                "plan_uuid": plan_uuid,
                "format": "minimal",
                **summary,
            }
        except Exception as e:
            return fail_obj(error=f"Failed to compute task summary: {e}")

    # Default: structured
    try:
        parsed = parse_tasks_md(content)
        return {
            "success": True,
            "plan_uuid": plan_uuid,
            "format": "structured",
            "phases": parsed["phases"],
        }
    except Exception as e:
        return fail_obj(error=f"Failed to parse tasks.md: {e}")


# ── Single Task Update ────────────────────────────────────────────────────


async def update_task_status(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    task_path: str = "",
    new_status: str = "",
) -> dict[str, Any]:
    """
    Update the status marker of a specific task in tasks.md.

    Args:
        workspace_path: Absolute path to the project workspace root.
        project_id: Project ID for agent-recall isolation.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        task_path: Phase/task path (e.g., "1.2").
        new_status: The target status marker (e.g., "[x]").

    Returns full mutation metadata including old_status, pre_mutation_state,
    file_path, db_synced status, and degraded mode fallback.
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(uuid=plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")
    if not validate_status(status=new_status):
        return fail_obj(error=f"Invalid status marker '{new_status}'.")

    tasks_path = settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=plan_uuid)
    content = read_utf8(path=tasks_path)

    if content is None:
        return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}'")

    pre_mutation_state = content

    # Resolve old status and validate transition
    old_status = _extract_old_status(content, task_path)
    if old_status is not None:
        transition_check = validate_status_transition(old_status, new_status)
        if not transition_check["valid"]:
            return {
                "success": False,
                "error": transition_check["reason"],
                "old_status": old_status,
                "valid_targets": transition_check["valid_targets"],
            }

    # Apply mutation
    try:
        updated_content = update_task_status_in_md(content, task_path, new_status)
    except Exception as e:
        return fail_obj(error=f"Failed to update task status: {e}")

    if updated_content is None:
        return fail_obj(error=f"Task '{task_path}' not found in plan '{plan_uuid}'")

    # Atomic file write with pending-queue fallback
    file_write_ok = write_utf8(path=tasks_path, content=updated_content)

    if not file_write_ok:
        pending_entry = {
            "type": "update_task_status",
            "plan_uuid": plan_uuid,
            "task_path": task_path,
            "new_status": new_status,
            "old_status": old_status,
            "pre_mutation_state": pre_mutation_state,
        }
        pending_ok = append_pending(workspace_path=workspace_path, entry=pending_entry)
        return {
            "success": False,
            "error": "Failed to write tasks.md. Operation queued to pending.jsonl.",
            "old_status": old_status,
            "file_path": str(tasks_path),
            "db_synced": False,
            "pre_mutation_state": pre_mutation_state,
            "pending_queued": pending_ok,
        }

    # Non-blocking DB sync
    db_synced = sync_to_agent_recall(
        workspace_path=workspace_path,
        plan_uuid=plan_uuid,
        updates=[{"task_path": task_path, "new_status": new_status}],
        project_id=project_id,
    )

    return {
        "success": True,
        "old_status": old_status,
        "new_status": new_status,
        "file_path": str(tasks_path),
        "db_synced": db_synced,
        "pre_mutation_state": pre_mutation_state,
    }


# ── Batch Task Update ─────────────────────────────────────────────────────


def _rollback_and_fail(
    tasks_path: str,
    workspace_path: str | Path,
    pre_mutation_state: str,
    successful: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> dict[str, Any]:
    """Roll back to pre-mutation state and return failure response."""
    write_utf8(path=tasks_path, content=pre_mutation_state)
    # Rollback undoes all partial successes; report nothing as successful.
    return _build_failure_response(False, [], failed, pre_mutation_state, rolled_back=True)


async def batch_update_tasks(
    workspace_path: str,
    project_id: str | None = None,
    plan_uuid: str = "",
    updates: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Atomically update multiple tasks in a plan's tasks.md.
    If any single update fails, ALL changes are rolled back.

    Each update may be ``{"task_path", "new_status"}`` (update existing) or
    ``{"task_path", "new_status", "description"}`` (auto-create the phase/task
    chain when the task does not exist). Internal transition validation returns
    ``valid_targets`` on illegal transitions.

    Args:
        workspace_path: Absolute path to the project workspace root.
        project_id: Project ID for agent-recall isolation.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        updates: List of {task_path, new_status[, description]} dicts.
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(uuid=plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    if not isinstance(updates, list) or not all(isinstance(u, dict) for u in updates):
        return fail_obj(error="Updates must be a list of dicts with 'task_path' and 'new_status'.")

    tasks_path = str(settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=plan_uuid))
    content = read_utf8(path=tasks_path)

    if content is None:
        return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}'")
    assert content is not None

    pre_mutation_state = content
    successful: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    current_content = content

    # Process each update atomically
    for update in updates:
        new_content, error = _process_single_update(
            workspace_path=workspace_path,
            current_content=current_content,
            update=update,
            tasks_path=tasks_path,
            pre_mutation_state=pre_mutation_state,
            successful=successful,
            executed=executed,
            skipped=skipped,
            created=created,
            failed=failed,
        )
        if error is not None:
            return error
        assert new_content is not None
        current_content = new_content

    # All updates succeeded — write final content
    assert current_content is not None
    file_ok = write_utf8(path=tasks_path, content=current_content)
    if not file_ok:
        return _build_failure_response(
            success=False,
            successful=successful,
            failed=[{"task_path": "FILE_WRITE", "new_status": "", "error": "Failed to write tasks.md"}],
            pre_mutation_state=pre_mutation_state,
            rolled_back=False,
        )

    db_synced = sync_to_agent_recall(
        workspace_path=workspace_path, plan_uuid=plan_uuid, updates=updates, project_id=project_id
    )

    return {
        "success": True,
        "successful": successful,
        "executed": executed,
        "skipped": skipped,
        "created": created,
        "failed": [],
        "db_synced": db_synced,
        "pre_mutation_state": pre_mutation_state,
    }


def _process_single_update(
    workspace_path: str | Path,
    current_content: str,
    update: dict[str, str],
    tasks_path: str,
    pre_mutation_state: str,
    successful: list[dict[str, Any]],
    executed: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    created: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Process a single update in a batch. Mutates trace lists in-place.

    Semantics:
        - invalid status      → failed, rolled back (valid_targets = all markers)
        - already-in-target   → skipped (idempotent no-op)
        - illegal transition  → failed, rolled back (with valid_targets)
        - task missing + desc → auto-create chain → created
        - task missing        → failed "Task not found" (rolled back)

    Returns:
        (updated_content, None) on success — caller must use the new content.
        (None, error_dict) on failure — rollback already performed.
    """
    task_path = update.get("task_path", "")
    new_status = update.get("new_status", "")
    description = update.get("description")

    if not validate_status(new_status):
        failed.append(
            {
                "task_path": task_path,
                "new_status": new_status,
                "error": f"Invalid status '{new_status}'",
                "valid_targets": sorted(VALID_STATUS_MARKERS),
            }
        )
        return None, _rollback_and_fail(tasks_path, workspace_path, pre_mutation_state, successful, failed)

    old_status = _extract_old_status(current_content, task_path)

    # Idempotent skip — already in the target state.
    if old_status == new_status:
        skipped.append({"task_path": task_path, "new_status": new_status, "reason": "already in target state"})
        return current_content, None  # no content change

    # Internal transition validation (existing task).
    if old_status is not None:
        transition_check = validate_status_transition(old_status, new_status)
        if not transition_check["valid"]:
            failed.append(
                {
                    "task_path": task_path,
                    "new_status": new_status,
                    "old_status": old_status,
                    "error": transition_check["reason"],
                    "valid_targets": transition_check["valid_targets"],
                }
            )
            return None, _rollback_and_fail(tasks_path, workspace_path, pre_mutation_state, successful, failed)

    try:
        updated = update_task_status_in_md(current_content, task_path, new_status)
    except Exception as e:
        failed.append({"task_path": task_path, "new_status": new_status, "error": str(e)})
        return None, _rollback_and_fail(tasks_path, workspace_path, pre_mutation_state, successful, failed)

    if updated is None:
        # Task missing — auto-create when a description is provided.
        if description:
            try:
                updated, created_path = create_task_in_md(
                    current_content,
                    task_path,
                    description=description,
                    new_status=new_status,
                )
            except Exception as e:
                failed.append({"task_path": task_path, "new_status": new_status, "error": f"Auto-create failed: {e}"})
                return None, _rollback_and_fail(tasks_path, workspace_path, pre_mutation_state, successful, failed)
            if updated is None:
                failed.append({"task_path": task_path, "new_status": new_status, "error": "Task not found"})
                return None, _rollback_and_fail(tasks_path, workspace_path, pre_mutation_state, successful, failed)
            created.append({"task_path": task_path, "description": description, "new_status": new_status})
            successful.append({"task_path": task_path, "old_status": None, "new_status": new_status})
            return updated, None
        failed.append({"task_path": task_path, "new_status": new_status, "error": "Task not found"})
        return None, _rollback_and_fail(tasks_path, workspace_path, pre_mutation_state, successful, failed)

    successful.append({"task_path": task_path, "old_status": old_status, "new_status": new_status})
    executed.append({"task_path": task_path, "old_status": old_status, "new_status": new_status})
    return updated, None  # signal success


async def write_plan_tasks(
    workspace_path: str,
    plan_uuid: str,
    content: str,
) -> dict[str, Any]:
    """Write content to a plan's tasks.md.

    Args:
        workspace_path: Absolute path to the project workspace root.
        plan_uuid: 8-character lowercase alphanumeric UUID of the plan.
        content: Full markdown content to write to tasks.md.

    Returns:
        {success, file_path} or error dict.
    """
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    tasks_path = settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=plan_uuid)
    ok = write_utf8(path=tasks_path, content=content)
    if not ok:
        return fail_obj(error=f"Failed to write tasks.md at {tasks_path}")
    return {"success": True, "file_path": str(tasks_path)}
