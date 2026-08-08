"""
File I/O, registry operations, pending queue, and agent-recall sync helpers.

Extracted from the original monolithic plan_tools.py. Provides all low-level
file, registry, and memory-sync operations used by the higher-level modules.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    add_observations,
    compute_tasks_summary,
    create_entities,
    fail_obj,
    logger,
    parse_registry,
    read_utf8,
    rebuild_registry_content,
    validate_workspace_path,
    write_utf8,
)

# ── Pending Queue (offline cache — avoid stale/lost state) ─────────────────
#
# `pending.jsonl` is the OFFLINE CACHE: one JSON object per line (JSONL) at
# `.ai/memory-bank/pending.jsonl`. It holds mutations that could not be
# applied to their store (tasks.md file write, agent-recall DB sync, memory
# write) so nothing is silently lost when a store is down. `mem_replay` drains
# it; agents also queue directly here when the MCP server itself is unreachable
# (see rule 14-mcp-offline-cache).


def pending_path(workspace_path: str | Path) -> Path:
    """Location of the offline-cache queue (``.ai/memory-bank/pending.jsonl``)."""
    return settings.get_memory_bank_dir(workspace_path) / "pending.jsonl"


def append_pending(workspace_path: str | Path, entry: dict[str, Any]) -> bool:
    """Append a failed operation to pending.jsonl (JSONL) for later replay."""
    try:
        path = pending_path(workspace_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return True
    except Exception:
        return False


def read_pending(workspace_path: str | Path) -> list[dict[str, Any]]:
    """Read queued entries (one JSON object per line; corrupt lines skipped)."""
    try:
        path = pending_path(workspace_path)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue  # tolerate a torn/corrupt tail line
            if isinstance(obj, dict):
                out.append(obj)
        return out
    except Exception:
        return []


def replace_pending(workspace_path: str | Path, entries: list[dict[str, Any]]) -> bool:
    """Overwrite pending.jsonl with ``entries`` (e.g. keep only failures after replay)."""
    try:
        path = pending_path(workspace_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, default=str) + "\n")
        return True
    except Exception:
        return False


def clear_pending(workspace_path: str | Path) -> bool:
    """Remove the offline cache entirely (after a successful replay)."""
    try:
        path = pending_path(workspace_path)
        if path.exists():
            path.unlink()
        return True
    except Exception:
        return False


# ── Registry Helpers ───────────────────────────────────────────────────────


def get_phase_summary_from_registry(
    workspace_path: str | Path,
    plan_uuid: str,
) -> dict[str, Any]:
    """
    Extract phase summary info from a plan's tasks.md for registry update.

    Args:
        workspace_path: Project root path. If empty, falls back to CWD.
        plan_uuid: The plan UUID.

    Returns:
        {tasks_complete: int, tasks_total: int}
    """
    tasks_path = settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=plan_uuid)
    content = read_utf8(path=tasks_path)
    if content is None:
        return {"tasks_complete": 0, "tasks_total": 0}

    summary = compute_tasks_summary(content)
    return {
        "tasks_complete": summary["terminal"],
        "tasks_total": summary["total"],
    }


def update_registry_phase_count(workspace_path: str | Path, plan_uuid: str) -> bool:
    """
    Update the registry with latest phase completion counts.
    Rewrites the summary field to include completion counts.

    Args:
        workspace_path: Optional project root. Falls back to CWD.
        plan_uuid: The plan UUID to update.
    """
    try:
        root = Path(workspace_path) if workspace_path else Path.cwd()
        registry = parse_registry(root)
        if not registry.get("success", False):
            return False

        summary_data = get_phase_summary_from_registry(plan_uuid=plan_uuid, workspace_path=workspace_path)
        new_summary_suffix = f" (Phase complete: {summary_data['tasks_complete']}/{summary_data['tasks_total']} tasks)"

        found = False
        for table_name in ("active", "paused", "completed"):
            for entry in registry.get(table_name, []):
                if entry["uuid"] == plan_uuid:
                    base = entry["summary"].split(" (Phase complete:")[0]
                    entry["summary"] = base + new_summary_suffix
                    found = True
                    break
            if found:
                break

        if not found:
            return False

        new_content = rebuild_registry_content(
            registry.get("active", []),
            registry.get("paused", []),
            registry.get("completed", []),
        )
        return write_utf8(
            root / ".ai" / "artifacts" / "registry.md",
            new_content,
        )
    except Exception:
        logger.error("Error to update registry phase count.")
        return False


# ── Agent-Recall Sync ─────────────────────────────────────────────────────


def sync_to_agent_recall(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    updates: list[dict[str, str]] | None = None,
) -> bool:
    """Sync task updates to agent-recall DB. Returns True if successful."""
    # Validate inputs
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        logger.error(f"sync_to_agent_recall: {err}")
        return False

    try:
        if not updates:
            logger.error("sync_to_agent_recall: updates is empty")
            return False
        obs_contents = [f"Batch update: {u['task_path']} \u2192 {u['new_status']}" for u in updates]
        obs = [
            {
                "entityName": f"plan_{plan_uuid}",
                "contents": obs_contents,
            }
        ]
        add_observations(workspace_path=workspace_path, observations=obs, project_id=project_id)
        return True
    except Exception:
        logger.error("Error sync to agent recall.")
        return False


def store_memory_checkpoint(
    workspace_path: str | Path,
    project_id: str | None = None,
    plan_uuid: str = "",
    phase_num: int = 0,
    message: str = "",
) -> dict[str, Any]:
    """Store a memory checkpoint via agent-recall add_observations."""
    # Validate inputs
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)

    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        obs_contents = [
            f"Checkpoint: Phase {phase_num} completed at {timestamp}",
            f"Message: {message}",
        ]
        obs = [
            {
                "entityName": f"plan_{plan_uuid}",
                "contents": obs_contents,
            }
        ]
        add_observations(workspace_path=workspace_path, observations=obs, project_id=project_id)
        return {"success": True}
    except Exception as e:
        logger.error("Error to store memory checkpoint.")
        return fail_obj(error=str(e))


def _pattern_name(pattern_type: str, value: str) -> str:
    """Stable, deterministic pattern entity name.

    Uses sha1 of the value so the SAME convention always maps to the SAME entity
    across projects and sessions (unlike Python's built-in ``hash()``, which is
    randomized per process and would create duplicate entities). This is what
    makes cross-project dedup/reuse work.
    """
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"pattern_{pattern_type}_{digest}"


def _detect_stack(workspace_path: str | Path) -> str:
    """Best-effort project stack label (framework → language → 'any')."""
    try:
        from ..context_tools.scanner import detect_framework

        info = detect_framework(str(workspace_path))
        fw = info.get("framework")
        if fw and fw != "Unknown":
            return fw
        langs = (info.get("all_detected") or {}).get("languages", [])
        if langs:
            return langs[0]
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return "any"


def _project_slug(workspace_path: str | Path) -> str:
    """Project slug: project-id if set, else the directory name."""
    try:
        pid = settings.get_project_id(workspace_path)
        if pid:
            return pid
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return Path(workspace_path).name


def store_pattern_entity(
    workspace_path: str | Path,
    project_id: str | None = None,
    name: str = "",
    observation: str = "",
    pattern_type: str = "",
    patterns: bool = False,
    stack: str = "",
    context: str = "",
    source_project: str = "",
) -> dict[str, Any]:
    """Store a pattern entity via agent-recall.

    ``patterns=True`` routes to the dedicated user-patterns store so learned
    patterns are cross-project (the agent grows with the user), not tied to one
    project's memory db. Records stack / context / source_project provenance so
    retrieval can scope by stack and the agent can judge applicability.
    """
    # Validate inputs
    valid, err = validate_workspace_path(workspace_path)
    if not valid:
        return fail_obj(error=err)

    try:
        create_entities(
            workspace_path=workspace_path,
            entities=[{"name": name, "entityType": "pattern"}],
            project_id=project_id,
            patterns=patterns,
        )
        add_observations(
            workspace_path=workspace_path,
            observations=[
                {
                    "entityName": name,
                    "contents": [
                        f"type: {pattern_type}",
                        f"value: {observation}",
                        "confidence: 0.9",
                        "source: retrospective",
                        f"stack: {stack or _detect_stack(workspace_path)}",
                        f"context: {context}",
                        f"source_project: {source_project or _project_slug(workspace_path)}",
                        f"timestamp: {datetime.now(timezone.utc).isoformat()}",
                    ],
                }
            ],
            project_id=project_id,
            patterns=patterns,
        )
        return {"success": True, "entity_name": name}
    except Exception as e:
        logger.error("Error to store pattern entity.")
        return fail_obj(error=str(e))
