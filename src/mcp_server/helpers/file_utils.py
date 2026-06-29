"""
File utilities for reading/writing markdown task files and parsing checklists.
"""

import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from ..config import settings
from .logger import logger
from .response import resp_obj, ok_obj, fail_obj


def read_file_safe(path: Path | str) -> str | None:
    """Read a file safely, returning None if it doesn't exist."""
    try:
        p: Path = Path(path) if isinstance(path, str) else path
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    except (PermissionError, OSError) as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)
        logger.error(f"Error reading {path}: {e}")
    return None


def _acquire_lock(lock_path: Path, timeout: float = 5.0) -> bool:
    """Acquire an exclusive lock file using O_CREAT|O_EXCL.

    Returns True if the lock was acquired, False on timeout.
    Retries with exponential backoff up to ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    delay = 0.05
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # Check if the lock is stale (process no longer exists)
            try:
                stale_pid = int(lock_path.read_text().strip())
                if stale_pid > 0:
                    try:
                        os.kill(stale_pid, 0)  # signal 0 = existence check
                    except ProcessLookupError:
                        # Stale lock — remove and retry
                        lock_path.unlink(missing_ok=True)
                        continue
            except (ValueError, OSError):
                pass
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    return False


def _release_lock(lock_path: Path) -> None:
    """Release a previously acquired lock file."""
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def write_file_safe(path: Path | str, content: str) -> bool:
    """Write content atomically with advisory file locking.

    Writes to a temporary file, then atomically replaces the target
    (``os.replace``) so readers always see a complete file.
    A ``.lock`` file prevents concurrent writes from different processes.
    """
    try:
        p: Path = Path(path) if isinstance(path, str) else path
        p.parent.mkdir(parents=True, exist_ok=True)
        lock_path = p.with_suffix(p.suffix + ".lock")

        if not _acquire_lock(lock_path):
            logger.error(f"Could not acquire lock for {path} (timeout)")
            return False

        try:
            # Write to temp file, then atomically replace target
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=p.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(content)
                    tmp.flush()
                    os.fsync(fd)  # Ensure data is on disk
                os.replace(tmp_path, str(p))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        finally:
            _release_lock(lock_path)

        return True
    except (PermissionError, OSError) as e:
        print(f"Error writing {path}: {e}", file=sys.stderr)
        logger.error(f"Error writing {path}: {e}")
    return False


# ── core file reader ────────────────────────────────────────────────────────


def read_registry_md(workspace_path: str | Path = "") -> dict[str, Any]:
    """Read a registry.md file."""
    registry_path = settings.get_registry_path(workspace_path=workspace_path)
    content = read_file_safe(registry_path)
    if content is None:
        # Auto create registry.md with empty table if not exist
        from .registry_utils import rebuild_registry_content
        content = rebuild_registry_content(active=[], paused=[], completed=[])
        write_file_safe(path=registry_path, content=content)

        # now read_registry_md always return status: True with empty table content
        # return {"success": False, "error": f"registry.md not found"}
    return ok_obj(content=content, path=str(registry_path))


def read_plan_md(workspace_path: str | Path = "", uuid: str = "") -> dict[str, Any]:
    """Read a plan's plan.md file."""
    plan_path = settings.get_plan_path(workspace_path=workspace_path, plan_uuid=uuid)
    content = read_file_safe(plan_path)
    if content is None:
        return fail_obj(error=f"plan.md not found for {uuid}")
    return resp_obj(content=content, path=str(plan_path))


def read_tasks_md(workspace_path: str | Path = "", uuid: str = "") -> dict[str, Any]:
    """Read a plan's tasks.md file."""
    tasks_path = settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=uuid)
    content = read_file_safe(str(tasks_path))
    if content is None:
        return fail_obj(error=f"tasks.md not found for {uuid}")
    return resp_obj(content=content, path=str(tasks_path))


# ── tasks.md parser ──────────────────────────────────────────────────────────


def parse_tasks_md(content: str) -> dict[str, Any]:
    """
    Parse a tasks.md file into a structured JSON representation.

    Returns:
        {
            "phases": [
                {
                    "name": "Phase 1: Backend Auth",
                    "phase_number": 1,
                    "tasks": [
                        {
                            "description": "Task description",
                            "status": "[ ]",
                            "indent": 0,
                            "subtasks": []
                        }
                    ]
                }
            ]
        }
    """
    phases: list[dict[str, Any]] = []
    current_phase: dict[str, Any] | None = None
    task_stack: list[dict[str, Any]] = []

    phase_pattern = re.compile(r"^##\s+Phase\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)
    task_pattern = re.compile(r"^(\s*)(- )(\[[ x✓!—⏳]+\])\s*(.+)$")

    for line in content.splitlines():
        phase_match = phase_pattern.match(line)
        if phase_match:
            current_phase = {
                "name": line.strip().lstrip("#").strip(),
                "phase_number": int(phase_match.group(1)),
                "tasks": [],
            }
            phases.append(current_phase)
            task_stack = []
            continue

        if current_phase is None:
            continue

        task_match = task_pattern.match(line)
        if task_match:
            indent_str = task_match.group(1)
            indent = len(indent_str)
            status = task_match.group(3)
            description = task_match.group(4).strip()

            task_entry = {
                "description": description,
                "status": status,
                "indent": indent,
                "subtasks": [],
            }

            # Handle nesting based on indentation
            while task_stack and task_stack[-1]["indent"] >= indent:
                task_stack.pop()

            if task_stack:
                task_stack[-1]["subtasks"].append(task_entry)
            else:
                current_phase["tasks"].append(task_entry)

            task_stack.append(task_entry)

    return resp_obj(phases=phases)


def update_task_status_in_md(content: str, task_path: str, new_status: str) -> str | None:
    """
    Update the status marker of a specific task in tasks.md content.

    Args:
        content: The original tasks.md content.
        task_path: "1.2" means Phase 1, task index 2 (0-based: second task).
        new_status: The new status marker (e.g., "[x]", "[ ]", etc.)

    Returns:
        Updated content string, or None if the task was not found.
    """
    parts = task_path.split(".")
    if len(parts) != 2:
        return None
    try:
        target_phase = int(parts[0])
        target_task_idx = int(parts[1])
    except ValueError:
        return None

    phase_pattern = re.compile(r"^##\s+Phase\s+(\d+)\s*:", re.IGNORECASE)
    task_pattern = re.compile(r"^(\s*)(- )\[[ x✓!—⏳]+\](.*)$")

    lines = content.splitlines()
    current_phase_num = 0
    task_count_in_phase = 0
    found = False

    for i, line in enumerate(lines):
        phase_match = phase_pattern.match(line)
        if phase_match:
            current_phase_num = int(phase_match.group(1))
            task_count_in_phase = 0
            continue

        if current_phase_num == target_phase:
            task_match = task_pattern.match(line)
            if task_match:
                indent_str = task_match.group(1)
                # Only count top-level tasks (no leading whitespace)
                if len(indent_str) == 0:
                    task_count_in_phase += 1
                    if task_count_in_phase == target_task_idx:
                        prefix = task_match.group(1)
                        dash = task_match.group(2)
                        rest = task_match.group(3)
                        lines[i] = f"{prefix}{dash}{new_status}{rest}"
                        found = True
                        break

    if not found:
        return None

    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


# ── Task Query Helpers ───────────────────────────────────────────────────────


def get_task_status(content: str, task_path: str) -> str | None:
    """
    Get the current status marker of a specific task.

    Args:
        content: The tasks.md content.
        task_path: "1.2" for Phase 1, Task 2.

    Returns:
        Status string (e.g., "[x]") or None if not found.
    """
    parts = task_path.split(".")
    if len(parts) != 2:
        return None
    try:
        target_phase = int(parts[0])
        target_task_idx = int(parts[1])
    except ValueError:
        return None

    phase_pattern = re.compile(r"^##\s+Phase\s+(\d+)\s*:", re.IGNORECASE)
    task_pattern = re.compile(r"^(\s*)(- )\[[ x✓!—⏳]+\](.*)$")

    current_phase_num = 0
    task_count_in_phase = 0

    for line in content.splitlines():
        phase_match = phase_pattern.match(line)
        if phase_match:
            current_phase_num = int(phase_match.group(1))
            task_count_in_phase = 0
            continue

        if current_phase_num == target_phase:
            task_match = task_pattern.match(line)
            if task_match and len(task_match.group(1)) == 0:
                task_count_in_phase += 1
                if task_count_in_phase == target_task_idx:
                    return task_match.group(2).strip()

    return None


def get_tasks_in_phase(content: str, phase_num: int) -> list[dict[str, Any]] | None:
    """
    Get all top-level tasks in a specific phase with their statuses and descriptions.

    Returns:
        List of dicts: [{"index": 1, "description": "...", "status": "[ ]"}]
        or None if phase not found.
    """
    parsed = parse_tasks_md(content)
    for phase in parsed["phases"]:
        if phase["phase_number"] == phase_num:
            result = []
            for i, task in enumerate(phase["tasks"]):
                # Extract dependency info from description
                deps = []
                desc = task["description"]
                dep_match = re.search(r"→\s*depends:\s*(.+?)$", desc)
                if dep_match:
                    deps_raw = dep_match.group(1).strip()
                    deps = [d.strip() for d in deps_raw.split(",")]

                result.append({
                    "index": i + 1,
                    "description": desc,
                    "status": task["status"],
                    "dependencies": deps,
                    "subtask_count": len(task["subtasks"]),
                })
            return result

    return None


def has_incomplete_tasks_in_phase(content: str, phase_num: int) -> list[dict[str, Any]]:
    """
    Find all incomplete (`[ ]` or `[!]`) or deferred (`[⏳]`) tasks in a phase.

    Returns:
        List of blocking task dicts, or empty list if phase is complete.
    """
    tasks = get_tasks_in_phase(content, phase_num)
    if tasks is None:
        return []

    blocking = []

    for t in tasks:
        if t["status"] in ("[ ]", "[!]"):
            blocking.append(t)
        elif t["status"] == "[⏳]":
            blocking.append(t)

    return blocking


def get_next_eligible_task(content: str, phase_num: int) -> dict[str, Any] | None:
    """
    Find the next eligible (non-blocked, non-terminal) task in a phase.

    Uses dependency annotations: if a task has `→ depends: Task X` and
    that dependency is not `[x]` or `[—]`, marks this task as `[⏳]`.

    Returns:
        {
            "next_task": {...} | None,
            "deferred": [...],
            "completed": [...],
            "all_terminal": bool,
            "cascade_failure": bool
        }
    """
    tasks = get_tasks_in_phase(content, phase_num)
    if tasks is None:
        return {
            "next_task": None,
            "deferred": [],
            "completed": [],
            "all_terminal": True,
            "cascade_failure": False,
        }

    deferred = []
    completed = []
    next_task = None

    for t in tasks:
        if t["status"] in ("[x]", "[x✓]", "[x!]", "[—]"):
            completed.append(t)
            continue

        if t["status"] == "[⏳]":
            # Re-evaluate: check if dependencies are now met
            deps_met = True
            for dep_name in t["dependencies"]:
                # Scan all tasks for the dependency
                dep_found = False
                for other in tasks:
                    if dep_name.lower() in other["description"].lower():
                        dep_found = True
                        if other["status"] not in ("[x]", "[x✓]", "[x!]", "[—]"):
                            deps_met = False
                        break
                if not dep_found:
                    deps_met = False

            if deps_met:
                next_task = t
            else:
                deferred.append(t)
            continue

        # Check dependencies for pending task
        deps_met = True
        for dep_name in t["dependencies"]:
            dep_found = False
            for other in tasks:
                if dep_name.lower() in other["description"].lower():
                    dep_found = True
                    if other["status"] not in ("[x]", "[x✓]", "[x!]", "[—]"):
                        deps_met = False
                    break
            if not dep_found:
                deps_met = False

        if deps_met:
            next_task = t
            break  # Found first eligible
        else:
            deferred.append(t)

    all_terminal = next_task is None and len(deferred) == 0
    cascade_failure = next_task is None and len(deferred) > 0 and len(tasks) == len(completed) + len(deferred)

    result = {
        "next_task": next_task,
        "deferred": deferred,
        "completed": completed,
        "all_terminal": all_terminal,
        "cascade_failure": cascade_failure,
    }
    return resp_obj(**result)


def compute_tasks_summary(content: str) -> dict[str, Any]:
    """
    Compute summary counts for all tasks across all phases.

    Returns:
        {
            "total": int,
            "completed": int,
            "terminal": int,
            "pending": int,
            "deferred": int,
            "failed": int,
            "skipped": int,
            "phase_summaries": [
                {"phase_number": 1, "name": "...", "total": 5, "completed": 3, ...}
            ]
        }
    """
    parsed = parse_tasks_md(content)
    all_tasks = []

    for phase in parsed["phases"]:
        flat_tasks = []
        def _flatten(task_list):
            for t in task_list:
                flat_tasks.append(t)
                _flatten(t["subtasks"])
        _flatten(phase["tasks"])

        phase_total = len(flat_tasks)
        phase_completed = sum(1 for t in flat_tasks if t["status"] in ("[x]", "[x✓]", "[x!]"))
        phase_pending = sum(1 for t in flat_tasks if t["status"] == "[ ]")
        phase_deferred = sum(1 for t in flat_tasks if t["status"] == "[⏳]")
        phase_failed = sum(1 for t in flat_tasks if t["status"] == "[!]")
        phase_skipped = sum(1 for t in flat_tasks if t["status"] == "[—]")
        phase_terminal = phase_completed + phase_skipped

        all_tasks.extend(flat_tasks)

    total = len(all_tasks)
    completed = sum(1 for t in all_tasks if t["status"] in ("[x]", "[x✓]", "[x!]"))
    terminal = completed + sum(1 for t in all_tasks if t["status"] == "[—]")
    pending = sum(1 for t in all_tasks if t["status"] == "[ ]")
    deferred = sum(1 for t in all_tasks if t["status"] == "[⏳]")
    failed = sum(1 for t in all_tasks if t["status"] == "[!]")
    skipped = sum(1 for t in all_tasks if t["status"] == "[—]")

    phase_summaries = []
    for phase in parsed["phases"]:
        flat_tasks = []
        def _flatten_phase(task_list):
            for t in task_list:
                flat_tasks.append(t)
                _flatten_phase(t["subtasks"])
        _flatten_phase(phase["tasks"])

        phase_summaries.append({
            "phase_number": phase["phase_number"],
            "name": phase["name"],
            "total": len(flat_tasks),
            "completed": sum(1 for t in flat_tasks if t["status"] in ("[x]", "[x✓]", "[x!]")),
            "pending": sum(1 for t in flat_tasks if t["status"] == "[ ]"),
            "deferred": sum(1 for t in flat_tasks if t["status"] == "[⏳]"),
            "failed": sum(1 for t in flat_tasks if t["status"] == "[!]"),
            "skipped": sum(1 for t in flat_tasks if t["status"] == "[—]"),
        })

    result = {
        "total": total,
        "completed": completed,
        "terminal": terminal,
        "pending": pending,
        "deferred": deferred,
        "failed": failed,
        "skipped": skipped,
        "phase_summaries": phase_summaries,
    }
    return resp_obj(**result)


# ── memory bank file reading ──────────────────────────────────────────────────

ALLOWED_MEMORY_FILES = {"environment.md"}


def read_memory_bank_file(workspace_path: str | Path, filename: str) -> str | None:
    """Safely read an allowed file from the memory bank directory."""
    if filename not in ALLOWED_MEMORY_FILES:
        return None
    path = (settings.get_memory_bank_dir(workspace_path=workspace_path) / filename).resolve()
    resolved_root = Path(workspace_path).resolve() if isinstance(workspace_path, str) else workspace_path.resolve()
    if not str(path).startswith(str(resolved_root)):
        return None
    return read_file_safe(path)