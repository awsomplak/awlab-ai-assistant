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
from .response import fail_obj, ok_obj, resp_obj


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


def read_notes_md(workspace_path: str | Path = "", uuid: str = "") -> dict[str, Any]:
    """Read a plan's notes.md file (may not exist — optional artifact)."""
    notes_path = settings.get_plan_dir(workspace_path=workspace_path, plan_uuid=uuid) / "notes.md"
    content = read_file_safe(notes_path)
    if content is None:
        return fail_obj(error=f"notes.md not found for {uuid}")
    return resp_obj(content=content, path=str(notes_path))


# ── plan.md / notes.md parsers ──────────────────────────────────────────────


def _split_md_sections(content: str) -> list[dict[str, Any]]:
    """Split markdown into sections by ``##`` headings.

    Returns ``[{heading, body}]`` where ``body`` is the raw text between this
    heading and the next. A leading ``#`` title (before any ``##``) is captured
    as a section with ``heading=""`` when present.
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] = {"heading": "", "body": []}
    for line in content.splitlines():
        if line.startswith("## "):
            if current["body"]:
                sections.append(current)
            current = {"heading": line[3:].strip(), "body": []}
        else:
            current["body"].append(line)
    if current["body"] or current["heading"]:
        sections.append(current)
    return sections


def _section_bullets(body: str) -> list[str]:
    """Extract ``- `` bullets from a section body, stripped of leading markers."""
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
        elif s.startswith("* "):
            out.append(s[2:].strip())
    return out


def parse_plan_md(content: str) -> dict[str, Any]:
    """Parse a plan.md into structured fields (name, overview, approach, …).

    Section headings are normalized (lowercase, non-alphanumerics → ``_``) and
    each section's body + bullets are preserved. ``name`` comes from the ``#``
    title line when present.
    """
    title = ""
    for line in content.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            break

    sections: dict[str, Any] = {}
    for sec in _split_md_sections(content):
        key = re.sub(r"[^a-z0-9]+", "_", sec["heading"].lower()).strip("_") or "title"
        body = "\n".join(sec["body"]).strip()
        sections[key] = {
            "heading": sec["heading"],
            "body": body,
            "bullets": _section_bullets("\n".join(sec["body"])),
        }

    return resp_obj(
        name=title,
        sections=sections,
        overview=sections.get("overview", {}).get("body", ""),
        approach=sections.get("approach", {}).get("bullets", []),
        expected_outcomes=sections.get("expected_outcomes", {}).get("bullets", []),
    )


def parse_notes_md(content: str) -> dict[str, Any]:
    """Parse a notes.md into structured fields (key decisions, risks, …).

    Section headings are normalized the same way as ``parse_plan_md``. The
    common sections map to typed accessors: ``key_decisions``, ``constraints``,
    ``risks``, ``open_questions`` — each a list of bullets (falling back to the
    whole section body split on lines when no bullets exist).
    """
    title = ""
    for line in content.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            break

    sections: dict[str, Any] = {}
    for sec in _split_md_sections(content):
        key = re.sub(r"[^a-z0-9]+", "_", sec["heading"].lower()).strip("_") or "title"
        body = "\n".join(sec["body"]).strip()
        bullets = _section_bullets("\n".join(sec["body"]))
        sections[key] = {
            "heading": sec["heading"],
            "body": body,
            "bullets": bullets,
        }

    def _typed(key: str) -> list[str]:
        sec = sections.get(key)
        if not sec:
            return []
        if sec["bullets"]:
            return sec["bullets"]
        return [line for line in sec["body"].splitlines() if line.strip()]

    return resp_obj(
        name=title,
        sections=sections,
        key_decisions=_typed("key_decisions"),
        constraints=_typed("constraints"),
        risks=_typed("risks"),
        open_questions=_typed("open_questions"),
    )


# ── tasks.md parser ──────────────────────────────────────────────────────────


# Metadata continuation lines attached to the previous task (block style).
#   - `    → depends: Task 1, Task 2`   (dependencies — refs resolved by path or name)
#   - `    ? if: condition_met`          (conditional gate)
#   - `    → DONE: ...` / `notes: ...`   (annotation / completion note)
_DEPENDS_RE = re.compile(r"^\s*→\s*depends:\s*(.+?)\s*$")
_IF_RE = re.compile(r"^\s*\?\s*if:\s*(.+?)\s*$")
_NOTE_RE = re.compile(r"^\s*→\s*(?:DONE|NOTE[S]?)\s*:\s*(.+?)\s*$", re.IGNORECASE)


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
                            "path": "1.1",
                            "description": "Task description",
                            "status": "[ ]",
                            "indent": 0,
                            "depends": ["1.2"],
                            "if": "condition_met",
                            "notes": ["..."],
                            "subtasks": []
                        }
                    ]
                }
            ]
        }

    ``path`` is the N-level dotted path (``1.2.3``) matching the resolver in
    ``update_task_status_in_md`` / ``get_task_status``, so ``task_read`` and
    ``task_update`` always agree on how to address a task.

    Extended metadata (block style) is captured from indented continuation
    lines below a task: ``→ depends:`` (comma-separated refs), ``? if:``
    (conditional), ``→ DONE:`` / ``notes:`` (annotations). Inline ``→ depends:``
    on the task line itself is also parsed. Metadata is preserved so the format
    is round-trip safe through the generator.
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

            # Resolve dotted path via the same indentation walk used by the
            # status resolver, so task_read and task_update agree on paths.
            while task_stack and task_stack[-1]["indent"] >= indent:
                task_stack.pop()

            if task_stack:
                parent_path = task_stack[-1]["path"]
            else:
                parent_path = str(current_phase["phase_number"])
            sibling = 1
            for sib in task_stack[-1]["subtasks"] if task_stack else current_phase["tasks"]:
                if sib.get("path", "").startswith(f"{parent_path}."):
                    sibling = max(sibling, int(sib["path"].split(".")[-1]) + 1)
            path = f"{parent_path}.{sibling}"

            # Inline `→ depends:` on the task line (legacy style).
            depends: list[str] = []
            inline_dep = re.search(r"→\s*depends:\s*(.+?)\s*$", description)
            if inline_dep:
                depends = [d.strip() for d in inline_dep.group(1).split(",") if d.strip()]
                description = description[: inline_dep.start()].rstrip()

            task_entry = {
                "path": path,
                "description": description,
                "status": status,
                "indent": indent,
                "depends": depends,
                "if": "",
                "notes": [],
                "subtasks": [],
            }

            if task_stack:
                task_stack[-1]["subtasks"].append(task_entry)
            else:
                current_phase["tasks"].append(task_entry)

            task_stack.append(task_entry)
            continue

        # Block-style metadata lines attach to the nearest task (task_stack top).
        if task_stack:
            dep_match = _DEPENDS_RE.match(line)
            if dep_match:
                refs = [d.strip() for d in dep_match.group(1).split(",") if d.strip()]
                task_stack[-1]["depends"].extend(refs)
                continue
            if_match = _IF_RE.match(line)
            if if_match:
                task_stack[-1]["if"] = if_match.group(1).strip()
                continue
            note_match = _NOTE_RE.match(line)
            if note_match:
                task_stack[-1]["notes"].append(note_match.group(1).strip())
                continue

    return resp_obj(phases=phases)


_PHASE_RE = re.compile(r"^##\s+Phase\s+(\d+)\s*:", re.IGNORECASE)
_TASK_RE = re.compile(r"^(\s*)(- )(\[[ x✓!—⏳]+\])(.*)$")


def _build_task_path_map(content: str) -> dict[str, int]:
    """Map a dotted task path (N-level, e.g. ``1.2.3``) → line index.

    Path semantics: ``1.2.3`` = Phase 1, top-level task 2, subtask 3.
    Resolution is indentation-based: each task line is nested under the nearest
    preceding task line with a smaller indent. Top-level tasks are indexed within
    their phase; subtasks are indexed within their parent task. Returns
    ``{dotted_path: line_index}`` for every task line (including subtasks).
    """
    path_map: dict[str, int] = {}
    lines = content.splitlines()
    current_phase = 0
    # Stack of (indent, path) for the current nesting chain.
    stack: list[tuple[int, str]] = []
    # Per-parent sibling counter: parent_path -> count (phase number for top-level).
    sibling: dict[str, int] = {}

    for i, line in enumerate(lines):
        phase_match = _PHASE_RE.match(line)
        if phase_match:
            current_phase = int(phase_match.group(1))
            stack = []
            sibling = {}
            continue

        task_match = _TASK_RE.match(line)
        if not task_match or current_phase == 0:
            continue

        indent = len(task_match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()

        if stack:
            parent_path = stack[-1][1]
        else:
            parent_path = str(current_phase)
        key = parent_path
        sibling[key] = sibling.get(key, 0) + 1
        path = f"{key}.{sibling[key]}"
        path_map[path] = i
        stack.append((indent, path))

    return path_map


def _resolve_task_line(content: str, task_path: str) -> int | None:
    """Return the line index of a dotted task path, or None if not found."""
    path_map = _build_task_path_map(content)
    return path_map.get((task_path or "").strip())


def update_task_status_in_md(content: str, task_path: str, new_status: str) -> str | None:
    """
    Update the status marker of a specific task in tasks.md content.

    Args:
        content: The original tasks.md content.
        task_path: Multi-level dotted path, e.g. "1.2" (Phase 1, task 2) or
            "1.2.3" (Phase 1, task 2, subtask 3). Nested via indentation.
        new_status: The new status marker (e.g., "[x]", "[ ]", etc.)

    Returns:
        Updated content string, or None if the task was not found.
    """
    line_index = _resolve_task_line(content, task_path)
    if line_index is None:
        return None

    lines = content.splitlines()
    task_match = _TASK_RE.match(lines[line_index])
    if not task_match:
        return None
    prefix = task_match.group(1)
    dash = task_match.group(2)
    rest = task_match.group(4)
    lines[line_index] = f"{prefix}{dash}{new_status}{rest}"
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


# ── Task Query Helpers ───────────────────────────────────────────────────────


def get_task_status(content: str, task_path: str) -> str | None:
    """
    Get the current status marker of a specific task.

    Args:
        content: The tasks.md content.
        task_path: Multi-level dotted path, e.g. "1.2" or "1.2.3".

    Returns:
        Status string (e.g., "[x]") or None if not found.
    """
    line_index = _resolve_task_line(content, task_path)
    if line_index is None:
        return None
    lines = content.splitlines()
    task_match = _TASK_RE.match(lines[line_index])
    if not task_match:
        return None
    # Status marker is group(3): the "[...]" bracket, e.g. "[x]".
    return task_match.group(3)


def detect_indent_step(content: str) -> int:
    """Detect the indentation step (spaces per nesting level) in tasks.md.

    Falls back to 4 (the convention used by templates) when content has no
    nested tasks.
    """
    indents = {len(m.group(1)) for m in (_TASK_RE.match(line) for line in content.splitlines()) if m}
    sorted_indents = sorted(i for i in indents if i > 0)
    if not sorted_indents:
        return 4
    gaps = [b - a for a, b in zip(sorted_indents, sorted_indents[1:])]
    return min(gaps) if gaps else sorted_indents[0]


def create_task_in_md(
    content: str,
    task_path: str,
    description: str,
    new_status: str = "[ ]",
) -> tuple[str | None, str | None]:
    """Create a missing task (and its phase/parent chain) in tasks.md content.

    Auto-creates the ``## Phase N:`` header and any missing ancestor tasks along
    the dotted path, then inserts the leaf task with *description* and
    *new_status*. Returns ``(updated_content, created_path)``, or ``(None, None)``
    when the task already exists or the path is malformed.
    """
    parts = task_path.strip().split(".")
    if len(parts) < 2 or not description:
        return None, None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None, None
    if nums[0] < 1 or any(n < 1 for n in nums[1:]):
        return None, None

    # Fast path: already exists — nothing to create.
    if _resolve_task_line(content, task_path) is not None:
        return None, None

    step = detect_indent_step(content)
    lines = content.splitlines()

    # 1. Ensure the phase header exists.
    phase_idx: int | None = None
    for i, line in enumerate(lines):
        m = _PHASE_RE.match(line)
        if m and int(m.group(1)) == nums[0]:
            phase_idx = i
            break
    if phase_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"## Phase {nums[0]}: Phase {nums[0]}")
        phase_idx = len(lines) - 1

    # 2. Ensure each level of the chain (parents then leaf).
    for level in range(1, len(nums)):
        parent_path = ".".join(str(n) for n in nums[:level])
        target_idx = nums[level]
        path_so_far = ".".join(str(n) for n in nums[: level + 1])
        indent = (level - 1) * step

        if _resolve_task_line("\n".join(lines), path_so_far) is not None:
            continue  # already exists at this level

        path_map = _build_task_path_map("\n".join(lines))
        prefix = parent_path + "."
        siblings = sorted(
            (int(p.rsplit(".", 1)[1]), idx)
            for p, idx in path_map.items()
            if p.startswith(prefix) and p.count(".") == parent_path.count(".") + 1
        )
        insert_after: int | None = None
        for sib_idx, line_idx in siblings:
            if sib_idx < target_idx:
                insert_after = line_idx
            else:
                break
        if insert_after is None:
            if level == 1:
                insert_after = phase_idx
            else:
                insert_after = path_map.get(parent_path)
                if insert_after is None:
                    return None, None

        is_leaf = level == len(nums) - 1
        desc = description if is_leaf else f"Task {path_so_far}"
        status = new_status if is_leaf else "[ ]"
        lines.insert(insert_after + 1, f"{' ' * indent}- {status} {desc}")

    return "\n".join(lines) + ("\n" if content.endswith("\n") else ""), task_path


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
                # Dependencies now come from the parser (covers both the inline
                # `→ depends:` style and the block-style indented line).
                result.append(
                    {
                        "index": i + 1,
                        "path": task.get("path", f"{phase_num}.{i + 1}"),
                        "description": task["description"],
                        "status": task["status"],
                        "dependencies": task.get("depends", []),
                        "subtask_count": len(task["subtasks"]),
                    }
                )
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


def resolve_dep_status(
    dep_ref: str,
    tasks: list[dict[str, Any]],
) -> str | None:
    """Resolve a dependency reference against the tasks of a phase.

    A reference may be:
      - a dotted path (``1.2`` / ``1.2.3``) → exact match on ``task["path"]``
      - a description fragment (legacy, e.g. ``Task 1``) → substring match

    Returns the referenced task's status marker, or ``None`` if unresolved.
    """
    ref = (dep_ref or "").strip()
    if not ref:
        return None

    # Exact dotted-path match first (canonical form).
    for t in tasks:
        if t.get("path") == ref:
            return t.get("status")

    # Fall back to description substring (legacy inline style).
    ref_lower = ref.lower()
    for t in tasks:
        if ref_lower in (t.get("description") or "").lower():
            return t.get("status")
    return None


def _deps_met(task: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
    """Return True when all of ``task``'s dependencies are terminal."""
    terminal = ("[x]", "[x✓]", "[x!]", "[—]")
    for dep_ref in task.get("dependencies", []):
        status = resolve_dep_status(dep_ref, tasks)
        if status is None or status not in terminal:
            return False
    return True


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
            if _deps_met(t, tasks):
                next_task = t
            else:
                deferred.append(t)
            continue

        # Check dependencies for pending task
        if _deps_met(t, tasks):
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

        phase_summaries.append(
            {
                "phase_number": phase["phase_number"],
                "name": phase["name"],
                "total": len(flat_tasks),
                "completed": sum(1 for t in flat_tasks if t["status"] in ("[x]", "[x✓]", "[x!]")),
                "pending": sum(1 for t in flat_tasks if t["status"] == "[ ]"),
                "deferred": sum(1 for t in flat_tasks if t["status"] == "[⏳]"),
                "failed": sum(1 for t in flat_tasks if t["status"] == "[!]"),
                "skipped": sum(1 for t in flat_tasks if t["status"] == "[—]"),
            }
        )

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

ALLOWED_MEMORY_FILES = {"environment.md", "context.md"}


def read_memory_bank_file(workspace_path: str | Path, filename: str) -> str | None:
    """Safely read an allowed file from the memory bank directory."""
    if filename not in ALLOWED_MEMORY_FILES:
        return None
    path = (settings.get_memory_bank_dir(workspace_path=workspace_path) / filename).resolve()
    resolved_root = Path(workspace_path).resolve() if isinstance(workspace_path, str) else workspace_path.resolve()
    if not str(path).startswith(str(resolved_root)):
        return None
    return read_file_safe(path)
