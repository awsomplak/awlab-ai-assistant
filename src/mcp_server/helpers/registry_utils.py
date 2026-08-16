"""
Registry utility for parsing and updating registry.md.

The registry has three tables: Active, Paused, Completed.
Each table has columns: UUID, Status, Date, Summary.

All public functions accept a workspace_path (str | Path) as first parameter.
"""

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from .file_utils import read_registry_md, write_file_safe
from .response import (
    fail_obj,
    ok_obj,
    resp_obj,
)

# Markers for section headers
ACTIVE_HEADER = "# Active Registry Plan"
PAUSED_HEADER = "# Paused Registry Plan"
COMPLETED_HEADER = "# Completed Registry Plan"
TABLE_HEADER = "| UUID | Status | Date | Created At | Summary |"
TABLE_SEPARATOR = "|------|--------|------|-----------|---------|"

TABLE_HEADER_PATTERN = re.compile(r"^\|?\s*(UUID|Status|Date|Summary)\s*\|")
TABLE_ROW_PATTERN = re.compile(r"^\|(.+)\|$")
TABLE_SEPARATOR_PATTERN = re.compile(r"^\|?\s*[-:]+\s*\|")


def _escape_cell(value: Any) -> str:
    """Escape a markdown table cell so a literal ``|`` doesn't split the column."""
    return str(value).replace("|", "\\|")


def _split_table_cells(stripped: str) -> list[str]:
    """Split a table row into cells, honoring ``\\|`` escapes.

    Splits on pipes *not* preceded by a backslash, drops the row's outer empty
    cells (from the leading/trailing ``|``), and unescapes ``\\|`` → ``|`` in
    every cell.
    """
    cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", stripped)]
    while cells and cells[0] == "":
        cells.pop(0)
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def parse_table_rows(content: str) -> list[dict[str, str]]:
    """
    Parse markdown table rows from a section of text.
    Returns a list of dicts with keys: uuid, status, date, summary.
    """
    rows: list[dict[str, str]] = []
    lines = content.strip().splitlines()
    header_found = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if TABLE_HEADER_PATTERN.match(stripped):
            header_found = True
            continue
        if TABLE_SEPARATOR_PATTERN.match(stripped):
            continue
        if header_found and TABLE_ROW_PATTERN.match(stripped):
            # Parse columns: split on unescaped pipes and trim. Canonical rows
            # have 5 cells (uuid, status, date, created_at, summary); legacy
            # 4-column rows (no Created At) are still accepted with created_at =
            # date. Literal `|` inside a cell is written as `\|` and unescaped
            # here; surplus cells (legacy rows with a raw `|` in the summary)
            # are rejoined into the summary so no content is lost.
            parts = _split_table_cells(stripped)
            if len(parts) >= 4:
                has_created = len(parts) >= 5
                rows.append(
                    {
                        "uuid": parts[0],
                        "status": parts[1],
                        "date": parts[2],
                        "created_at": parts[3] if has_created else parts[2],
                        "summary": "|".join(parts[4:] if has_created else parts[3:]),
                    }
                )
    return rows


def split_registry_sections(content: str) -> dict[str, str]:
    """
    Split registry content into three sections.
    Returns dict with keys 'active', 'paused', 'completed'.
    """
    sections: dict[str, str] = {}

    active_idx = content.find(ACTIVE_HEADER)
    paused_idx = content.find(PAUSED_HEADER)
    completed_idx = content.find(COMPLETED_HEADER)

    if active_idx >= 0 and paused_idx >= 0:
        sections["active"] = content[active_idx:paused_idx]
    elif active_idx >= 0:
        sections["active"] = content[active_idx:]

    if paused_idx >= 0 and completed_idx >= 0:
        sections["paused"] = content[paused_idx:completed_idx]
    elif paused_idx >= 0:
        sections["paused"] = content[paused_idx:]

    if completed_idx >= 0:
        sections["completed"] = content[completed_idx:]

    return sections


def parse_registry(workspace_path: str | Path) -> dict[str, Any]:
    """
    Parse the full registry.md into three lists.

    Args:
        workspace_path: Path to the project root directory.

    Returns:
        {
            "success": True/False,
            "active": [{ "uuid": "...", "status": "⏹️", "date": "...", "summary": "..." }],
            "paused": [...],
            "completed": [...]
        }
    """
    registry_md = read_registry_md(workspace_path=workspace_path)
    content = registry_md.get("content", None)

    result: dict[str, Any] = {
        "success": True,
        "active": [],
        "paused": [],
        "completed": [],
    }

    if content is None:
        return result

    try:
        sections = split_registry_sections(content)
        active = parse_table_rows(sections.get("active", ""))
        paused = parse_table_rows(sections.get("paused", ""))
        completed = parse_table_rows(sections.get("completed", ""))

        result["active"] = active
        result["paused"] = paused
        result["completed"] = completed
        return result
    except Exception:
        return result


def build_table_row(entry: dict[str, str]) -> str:
    """Build a markdown table row from an entry dict (incl. immutable Created At).

    Every cell is pipe-escaped (``|`` → ``\\|``) so summaries and other free-text
    fields containing ``|`` round-trip without breaking the table.
    """
    created_at = entry.get("created_at", entry.get("date", ""))
    cells = [
        _escape_cell(entry.get("uuid", "")),
        _escape_cell(entry.get("status", "")),
        _escape_cell(entry.get("date", "")),
        _escape_cell(created_at),
        _escape_cell(entry.get("summary", "")),
    ]
    return "| " + " | ".join(cells) + " |"


def rebuild_registry_content(
    active: list[dict[str, str]],
    paused: list[dict[str, str]],
    completed: list[dict[str, str]],
) -> str:
    """Rebuild the full registry.md content from three lists."""
    lines: list[str] = []

    # Active section
    lines.append(ACTIVE_HEADER)
    lines.append("")
    lines.append(TABLE_HEADER)
    lines.append(TABLE_SEPARATOR)
    for entry in active:
        lines.append(build_table_row(entry))
    lines.append("")

    # Paused section
    lines.append(PAUSED_HEADER)
    lines.append("")
    lines.append(TABLE_HEADER)
    lines.append(TABLE_SEPARATOR)
    for entry in paused:
        lines.append(build_table_row(entry))
    lines.append("")

    # Completed section
    lines.append(COMPLETED_HEADER)
    lines.append("")
    lines.append(TABLE_HEADER)
    lines.append(TABLE_SEPARATOR)
    for entry in completed:
        lines.append(build_table_row(entry))
    lines.append("")

    return "\n".join(lines)


def switch_active_plan(workspace_path: str | Path, target_uuid: str) -> dict[str, Any]:
    """
    Switch the active plan in the registry.

    Steps:
    1. Read registry from disk.
    2. Find current active entry and move it to paused.
    3. Find target entry (either in paused or completed) and move it to active.
    4. Rewrite registry.

    Args:
        workspace_path: Path to the project root directory.
        target_uuid: The UUID of the plan to activate.

    Returns:
        { "success": True, "new_active_uuid": target_uuid }
        or error dict.
    """
    registry_path: Path = settings.get_registry_path(workspace_path=workspace_path)
    if not registry_path.is_file():
        return fail_obj(error="registry.md is not found")

    registry = parse_registry(workspace_path=workspace_path)

    # Find current active entry
    current_active = registry["active"][0] if registry["active"] else None

    # If target is already active, it's a no-op success
    if current_active and current_active["uuid"] == target_uuid:
        return ok_obj(new_active_uuid=target_uuid)

    # Find target entry across all tables (skip active since we already checked)
    target_entry: dict[str, str] | None = None
    target_source: str | None = None

    for entry in registry["paused"]:
        if entry["uuid"] == target_uuid:
            target_entry = entry
            target_source = "paused"
            break

    if target_entry is None:
        for entry in registry["completed"]:
            if entry["uuid"] == target_uuid:
                target_entry = entry
                target_source = "completed"
                break

    if target_entry is None:
        return fail_obj(error=f"Plan with UUID '{target_uuid}' not found in registry")

    # Move current active to paused
    if current_active:
        current_active["status"] = "⏸️"
        registry["paused"].insert(0, current_active)

    # Remove target from its source
    if target_source == "paused":
        registry["paused"] = [e for e in registry["paused"] if e["uuid"] != target_uuid]
    elif target_source == "completed":
        registry["completed"] = [e for e in registry["completed"] if e["uuid"] != target_uuid]

    # Move target to active
    target_entry["status"] = "⏹️"
    registry["active"] = [target_entry]

    # Rebuild and write
    new_content = rebuild_registry_content(
        registry["active"],
        registry["paused"],
        registry["completed"],
    )

    if write_file_safe(registry_path, new_content):
        return ok_obj(new_active_uuid=target_uuid)
    else:
        return fail_obj(error="Failed to write registry.md")


def _new_uuid() -> str:
    """Generate an 8-char lowercase alphanumeric UUID (matches ``^[a-z0-9]{8}$``)."""
    return uuid.uuid4().hex[:8]


def create_registry_entry(workspace_path: str | Path, summary: str = "") -> dict[str, Any]:
    """Create a new registry row (server generates the UUID; Active table, ⏹️).

    Sets both ``date`` and ``created_at`` to now; ``created_at`` is immutable
    thereafter. ``rebuild_registry_content`` reformats registry.md.
    """
    registry_path: Path = settings.get_registry_path(workspace_path=workspace_path)
    if registry_path.is_file():
        registry = parse_registry(workspace_path=workspace_path)
    else:
        registry = {"active": [], "paused": [], "completed": []}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entry = {"uuid": _new_uuid(), "status": "⏹️", "date": now, "created_at": now, "summary": summary}
    registry["active"].insert(0, entry)
    new_content = rebuild_registry_content(registry["active"], registry["paused"], registry["completed"])
    if write_file_safe(registry_path, new_content):
        return ok_obj(created_uuid=entry["uuid"], table="active", date=now, created_at=now, summary=summary)
    return fail_obj(error="Failed to write registry.md")


_STATUS_MAP = {
    "active": ("⏹️", "active"),
    "paused": ("⏸️", "paused"),
    "complete": ("✅", "completed"),
}


def update_registry_status(
    workspace_path: str | Path,
    uuid: str,
    status: str,
    summary: str | None = None,
) -> dict[str, Any]:
    """Update a registry row's status: move to the correct table + refresh ``Date``.

    ``created_at`` is NEVER updated. ``summary`` is updated only when provided.
    ``Date`` reflects the last status change (creation counts as the first).
    """
    if status not in _STATUS_MAP:
        return fail_obj(error=f"Invalid status '{status}'. Valid: active, paused, complete")
    registry_path: Path = settings.get_registry_path(workspace_path=workspace_path)
    if not registry_path.is_file():
        return fail_obj(error="registry.md is not found")
    registry = parse_registry(workspace_path=workspace_path)

    entry: dict[str, str] | None = None
    source: str | None = None
    for table in ("active", "paused", "completed"):
        for e in registry[table]:
            if e["uuid"] == uuid:
                entry, source = e, table
                break
        if entry is not None:
            break
    if entry is None or source is None:
        return fail_obj(error=f"Plan with UUID '{uuid}' not found in registry")

    symbol, target = _STATUS_MAP[status]
    registry[source] = [e for e in registry[source] if e["uuid"] != uuid]
    entry["status"] = symbol
    entry["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if summary is not None:
        entry["summary"] = summary
    registry[target].insert(0, entry)

    new_content = rebuild_registry_content(registry["active"], registry["paused"], registry["completed"])
    if write_file_safe(registry_path, new_content):
        return ok_obj(
            updated_uuid=uuid,
            status=status,
            moved_from=source,
            moved_to=target,
            date=entry["date"],
            created_at=entry.get("created_at", entry["date"]),
        )
    return fail_obj(error="Failed to write registry.md")


def delete_registry_entry(workspace_path: str | Path, uuid: str) -> dict[str, Any]:
    """Delete a registry row from whichever table holds it."""
    registry_path: Path = settings.get_registry_path(workspace_path=workspace_path)
    if not registry_path.is_file():
        return fail_obj(error="registry.md is not found")
    registry = parse_registry(workspace_path=workspace_path)
    for table in ("active", "paused", "completed"):
        if any(e["uuid"] == uuid for e in registry[table]):
            registry[table] = [e for e in registry[table] if e["uuid"] != uuid]
            new_content = rebuild_registry_content(registry["active"], registry["paused"], registry["completed"])
            if write_file_safe(registry_path, new_content):
                return ok_obj(deleted_uuid=uuid, deleted_from=table)
            return fail_obj(error="Failed to write registry.md")
    return fail_obj(error=f"Plan with UUID '{uuid}' not found in registry")


# ── Convenience wrappers that accept workspace_path ─────────────────────────


def load_registry(workspace_path: str | Path = "") -> dict[str, list[dict[str, str]]]:
    """
    Load the full registry from disk.

    Args:
        workspace_path (str | Path): Path to the project root. If empty, falls back to CWD.

    Returns:
        dic[str, str]:
        { "active": [...], "paused": [...], "completed": [...] }

        Returns empty structure if registry not found.
    """
    registry = parse_registry(workspace_path=workspace_path)
    result = {
        "active": registry.get("active", []),
        "paused": registry.get("paused", []),
        "completed": registry.get("completed", []),
    }
    return resp_obj(**result)


def list_active_plans(workspace_path: str | Path = "") -> list[dict[str, str]]:
    """Return the list of active plan entries from the registry.

    Args:
        workspace_path (str | Path): Path to the project root. If empty, falls back to CWD.

    Returns:
        list[dict[str, str]]: List of dicts with uuid, status, date, summary.
        Returns empty list if registry not found or parse error.
    """
    registry = load_registry(workspace_path=workspace_path)
    return registry.get("active", [])


def list_paused_plans(workspace_path: str | Path = "") -> list[dict[str, str]]:
    """Return the list of paused plan entries from registry.

    Args:
        workspace_path (str | Path): Path to the project root. If empty, falls back to CWD.

    Returns:
        list[dict[str, str]]: List of dicts with uuid, status, date, summary.
        Returns empty list if registry not found or parse error.
    """
    registry = load_registry(workspace_path=workspace_path)
    return registry.get("paused", [])


def list_completed_plans(workspace_path: str | Path = "") -> list[dict[str, str]]:
    """Return the list of completed plan entries from registry.

    Args:
        workspace_path (str | Path): Path to the project root. If empty, falls back to CWD.

    Returns:
        list[dict[str, str]]: List of dicts with uuid, status, date, summary.
        Returns empty list if registry not found or parse error.
    """
    registry = load_registry(workspace_path=workspace_path)
    return registry.get("completed", [])
