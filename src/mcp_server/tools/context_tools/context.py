"""
Context snapshot, memory search, context store, and context fragment retrieval.

Provides tools that give the AI agent context awareness about the current
project, including plan status, memory search, and topic-specific context.
"""

from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    compute_tasks_summary,
    load_registry,
    read_plan_md,
    read_tasks_md,
    resolve_family_info,
)
from ._memory_search import query_agent_recall_for_patterns
from ._registry_parser import get_current_phase_from_tasks

# ── Tool Implementations ───────────────────────────────────────────────────


async def get_context_snapshot(workspace_path: str | Path = "") -> dict[str, Any]:
    """
    Read registry → find active plan → read plan.md + tasks.md →
    query agent-recall for recent patterns.

    Args:
        workspace_path: Project root path. If empty, falls back to CWD.

    Returns:
        { active_plan, patterns, project_id }
    """
    project_id = settings.get_project_id(workspace_path=workspace_path)
    registry = load_registry(workspace_path=workspace_path)
    active_list = registry.get("active", [])
    active_plan = active_list[0] if active_list else None

    plan_details = None
    tasks_summary = None

    if active_plan and active_plan.get("uuid"):
        uuid = active_plan["uuid"]
        plan_md = read_plan_md(uuid=uuid, workspace_path=workspace_path)
        tasks_md = read_tasks_md(uuid=uuid, workspace_path=workspace_path)

        if "error" not in plan_md:
            plan_details = {"content": plan_md.get("content", "")}
        if "error" not in tasks_md:
            raw_summary = compute_tasks_summary(content=tasks_md.get("content", ""))
            tasks_summary = {
                "total": raw_summary.get("total", 0),
                "completed": raw_summary.get("completed", 0),
                "pending": raw_summary.get("pending", 0),
                "deferred": raw_summary.get("deferred", 0),
                "failed": raw_summary.get("failed", 0),
                "skipped": raw_summary.get("skipped", 0),
                "current_phase": get_current_phase_from_tasks(content=tasks_md.get("content", "")),
                "phase_summaries": raw_summary.get("phase_summaries", []),
            }
            if plan_details:
                plan_details["tasks_summary"] = tasks_summary

    patterns = query_agent_recall_for_patterns(workspace_path=workspace_path, project_id=project_id, limit=5)

    # Project-family discovery: primary family (.ai/family-id) + every family this
    # workspace belongs to. Zero-discovery — the agent knows its family stores
    # (store="family_<slug>") without reading the global config.
    family = None
    try:
        finfo = resolve_family_info(workspace_path=workspace_path)
        if finfo.get("success"):
            family = {
                "family_id": finfo.get("family_id"),
                "workspace_family": finfo.get("workspace_family"),
                "workspace_families": finfo.get("workspace_families", []),
            }
    except Exception:  # noqa: BLE001 — discovery must never break the snapshot
        family = None

    return {
        "success": True,
        "active_plan": {
            "uuid": active_plan["uuid"] if active_plan else None,
            "summary": active_plan["summary"] if active_plan else None,
            "date": active_plan["date"] if active_plan else None,
            "plan_details": plan_details,
        }
        if active_plan
        else None,
        "patterns": patterns if patterns else [],
        "project_id": project_id,
        "family": family,
    }
