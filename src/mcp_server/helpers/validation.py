"""
Validation helpers for workspace, plan & task management.

Provides UUID format validation, status marker validation, and status transition
logic per the 02-plan-artifacts.md rules.
"""

import json
import re
from pathlib import Path
from typing import Any
from .response import fail_obj


# ── Constants ──────────────────────────────────────────────────────────────

# Files/directories that indicate a project root when found in a parent directory
_PROJECT_ROOT_MARKERS: set[str] = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    ".git",  # directory
    ".gitignore",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "composer.json",
    "Gemfile",
    "manage.py",
    "artisan",
    "next.config.js",
    "next.config.ts",
}


VALID_STATUS_MARKERS: set[str] = {"[ ]", "[x]", "[x✓]", "[x!]", "[!]", "[—]", "[⏳]"}


# Valid transitions: current → set of valid targets
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "[ ]": {"[x]", "[x✓]", "[x!]", "[!]", "[—]", "[⏳]"},
    "[x]": {"[x✓]", "[x!]", "[⏳]"},
    "[x✓]": {"[x!]"},
    "[x!]": set(),
    "[!]": {"[x]", "[x✓]", "[x!]", "[—]", "[⏳]"},
    "[—]": set(),
    "[⏳]": {"[ ]", "[x]", "[x✓]", "[x!]", "[!]", "[—]"},
}


# ── Public Helpers ─────────────────────────────────────────────────────────


def validate_workspace_path(workspace_path: str | Path | None) -> tuple[bool, str]:
    """
    Validate that *workspace_path* is non-empty and exists as a directory.

    Returns ``(True, "")`` if valid, or ``(False, "Invalid workspace path.")``
    if invalid.
    """
    if not workspace_path:
        return False, "Invalid workspace path."
    p = Path(workspace_path) if isinstance(workspace_path, str) else workspace_path
    if not p.exists() or not p.is_dir():
        return False, "Invalid workspace path."
    return True, ""


def validate_project_root(path: str | Path) -> bool:
    """Check if a path looks like a valid project root (has markers)."""
    p: Path = Path(path) if isinstance(path, str) else path
    if not p.is_dir():
        return False
    return any((p / marker).exists() for marker in _PROJECT_ROOT_MARKERS)


def validate_project_id(project_id: str | None) -> dict[str, bool | str] | None:
    """
    Validate that *project_id* is non-empty.

    Returns ``None`` if valid, or ``{"success": False, "error": "Invalid project ID."}``
    if invalid.
    """
    if not project_id:
        return fail_obj(error="Invalid project ID.")
    return None


def validate_uuid(uuid: str) -> bool:
    """
    Validate 8-char lowercase alphanumeric UUID format.

    Args:
        uuid: The UUID string to validate.

    Returns:
        True if the UUID matches ``[a-z0-9]{8}``, False otherwise.
    """
    return bool(re.match(r"^[a-z0-9]{8}$", uuid))


def validate_status(status: str) -> bool:
    """
    Validate a status marker.

    Args:
        status: The status marker string (e.g. ``[x]``, ``[ ]``).

    Returns:
        True if the status is a known marker, False otherwise.
    """
    return status in VALID_STATUS_MARKERS


def validate_status_transition(current: str, target: str) -> dict[str, Any]:
    """
    Validate whether a transition from *current* status to *target* status is legal.

    Args:
        current: The current status marker.
        target:  The desired new status marker.

    Returns:
        A dict with keys:
        - **valid** (bool): Whether the transition is allowed.
        - **reason** (str):  Human-readable explanation.
        - **valid_targets** (list[str]): Sorted list of valid targets from *current*.
    """
    if current not in STATUS_TRANSITIONS:
        return {
            "valid": False,
            "reason": f"Unknown current status '{current}'",
            "valid_targets": sorted(VALID_STATUS_MARKERS),
        }
    valid_targets = STATUS_TRANSITIONS[current]
    if target not in valid_targets:
        valid_list = sorted(valid_targets) if valid_targets else ["(none)"]
        return {
            "valid": False,
            "reason": (
                f"Cannot transition from '{current}' to '{target}'. "
                f"Valid targets: {', '.join(valid_list)}"
            ),
            "valid_targets": valid_list,
        }
    return {"valid": True, "reason": "", "valid_targets": sorted(valid_targets)}


# ── Shared validation error helpers ─────────────────────────────────────────


def require_uuid(plan_uuid: str) -> str | None:
    """Validate plan_uuid and return an error string if invalid, otherwise None."""
    return invalid_uuid() if not validate_uuid(plan_uuid) else None


def require_status(status: str) -> str | None:
    """Validate status and return an error string if invalid, otherwise None."""
    return invalid_status(status) if not validate_status(status) else None


def require_phase_number(phase_number: int) -> str | None:
    """Validate phase_number and return an error string if invalid, otherwise None."""
    return invalid_phase_number(phase_number) if phase_number < 1 else None


def invalid_uuid() -> str:
    """Return the standard invalid UUID error payload."""
    return ("Invalid plan_uuid format. Must be 8 lowercase alphanumeric characters.")


def invalid_status(status: str) -> str:
    """Return the standard invalid status error payload."""
    valid_markers = ", ".join(sorted(VALID_STATUS_MARKERS))
    return f"Invalid status '{status}'. Must be one of: {valid_markers}"


def invalid_phase_number(phase_number: int) -> str:
    """Return the standard invalid phase_number error payload."""
    return "phase_number must be >= 1."


def invalid_format(field: str, value: str, valid: set[str] | tuple[str, ...]) -> str:
    """Return the standard invalid format error payload."""
    return (f"Invalid {field} '{value}'. Must be one of: {', '.join(sorted(valid))}")


def invalid_scope(scope: str) -> str:
    """Return the standard invalid scope error payload."""
    return invalid_format("scope", scope, {"project", "user", "conversation"})


def invalid_status_marker(status: str, context: str) -> str:
    """Return a JSON error for an unknown status marker (used by transitions)."""
    return json.dumps(
        {
            "valid": False,
            "reason": f"Unknown {context} status '{status}'",
            "valid_targets": sorted(VALID_STATUS_MARKERS),
        }
    )

