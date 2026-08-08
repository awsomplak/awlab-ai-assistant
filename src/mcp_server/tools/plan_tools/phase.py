"""
Phase management tools — phase gate validation, phase listing, and
phase execution helpers.

Extracted from the original monolithic plan_tools.py.
"""

from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    fail_obj,
    parse_tasks_md,
    read_utf8,
    validate_uuid,
    validate_workspace_path,
)

# ── Phase Gate Validation ──────────────────────────────────────────────────


def _find_previous_phase(merged: dict[str, Any], previous_phase_num: int) -> dict[str, Any] | None:
    """Find the previous phase object from parsed tasks.md content."""
    for phase in merged["phases"]:
        if phase["phase_number"] == previous_phase_num:
            return phase
    return None


def _get_blocking_tasks(phase: dict[str, Any], phase_num: int) -> list[dict[str, Any]]:
    """Return a list of blocking (non-terminal) top-level tasks in a phase."""
    blocking: list[dict[str, Any]] = []
    for idx, task in enumerate(phase["tasks"], start=1):
        if task["indent"] == 0 and task["status"] in {"[ ]", "[⏳]", "[!]"}:
            blocking.append(
                {
                    "index": idx,
                    "task_path": f"{phase_num}.{idx}",
                    "description": task["description"],
                    "status": task["status"],
                }
            )
    return blocking


def _build_gate_pass(phase_num: int, msg: str) -> dict[str, Any]:
    """Build a pass response for phase gate validation."""
    return {
        "success": True,
        "pass": True,
        "blocking_tasks": [],
        "phase_complete": True,
        "reasons": [msg],
    }


def _build_gate_fail(reasons: list[str]) -> dict[str, Any]:
    """Build a fail response for phase gate validation."""
    return {
        "success": False,
        "pass": False,
        "blocking_tasks": [],
        "phase_complete": False,
        "reasons": reasons,
    }


async def validate_phase_gate(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    phase_num: int = 0,
) -> dict[str, Any]:
    """
    Validate that Phase {N-1} is complete before allowing Phase N to start.

    Args:
        workspace_path: Absolute path to the project workspace root.
        project_id: Optional project ID for agent-recall isolation.
        plan_uuid: 8-character lowercase alphanumeric UUID.
        phase_num: The phase number to check the gate for (e.g., 2 checks Phase 1).

    Returns:
        {success, pass, blocking_tasks, phase_complete, reasons}
    """
    # Validate workspace_path
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)
    if not validate_uuid(plan_uuid):
        return fail_obj(error="Invalid plan_uuid format.")

    try:
        tasks_path = settings.get_plan_tasks_path(workspace_path, plan_uuid)
    except Exception:
        return fail_obj(error="Invalid workspace path.")

    content = read_utf8(tasks_path)

    if content is None:
        return _build_gate_fail([f"tasks.md not found for plan '{plan_uuid}'"])

    if phase_num <= 1:
        return _build_gate_pass(1, "Phase 1 has no predecessor gate — automatically passes")

    previous_phase_num = phase_num - 1
    try:
        parsed = parse_tasks_md(content)
    except Exception as e:
        return _build_gate_fail([f"Failed to parse tasks.md: {e}"])

    previous_phase = _find_previous_phase(parsed, previous_phase_num)
    if previous_phase is None:
        return _build_gate_pass(
            previous_phase_num,
            f"Phase {previous_phase_num} not found — treating as complete",
        )

    blocking = _get_blocking_tasks(previous_phase, previous_phase_num)
    phase_complete = len(blocking) == 0

    return {
        "success": phase_complete,
        "pass": phase_complete,
        "blocking_tasks": blocking,
        "phase_complete": phase_complete,
        "reasons": ([] if phase_complete else [f"Phase {previous_phase_num} has {len(blocking)} incomplete task(s)."]),
    }
