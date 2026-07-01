"""
Context snapshot, memory search, context store, and context fragment retrieval.

Provides tools that give the AI agent context awareness about the current
project, including plan status, memory search, and topic-specific context.
"""

from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    load_registry,
    read_plan_md,
    read_tasks_md,
    compute_tasks_summary
)
from ._cache import load_cache, save_cache
from ._registry_parser import get_current_phase_from_tasks
from ._memory_search import (
    get_ttl_expiry,
    prune_expired_entries,
    build_expiry_timestamp,
    search_context_store,
    search_memory_for_topic,
    search_registry_for_topic,
    query_agent_recall_for_patterns,
)

# ── Cache file paths ───────────────────────────────────────────────────────

_CONTEXT_STORE = "context_store.json"


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

    patterns = query_agent_recall_for_patterns(
        workspace_path=workspace_path,
        project_id=project_id,
        limit=5
    )

    return {
        "success": True,
        "active_plan": {
            "uuid": active_plan["uuid"] if active_plan else None,
            "summary": active_plan["summary"] if active_plan else None,
            "date": active_plan["date"] if active_plan else None,
            "plan_details": plan_details,
        } if active_plan else None,
        "patterns": patterns if patterns else [],
        "project_id": project_id,
    }


async def store_context(
    workspace_path: str | Path,
    key: str,
    value: str,
    scope: str = "project",
    ttl: int = 3600,
) -> dict[str, Any]:
    """
    Store a key-value context entry with TTL-based auto-expiry and dedup.

    Args:
        key: Context key (e.g., "last_task_description", "current_branch").
        value: The value to store.
        scope: Scope — "project", "user", or "conversation".
        ttl: Time-to-live in seconds (default 3600 = 1 hour). Use 0 for no expiry.
        workspace_path: Project root path. If empty, falls back to CWD.

    Returns:
        { success, key, scope, deduplicated, expires_at }
    """
    ctx_path = get_context_path(workspace_path=workspace_path)
    stored = load_cache(workspace_path=workspace_path, cache_path=ctx_path)
    store = stored if stored is not None else {}
    expires_at = build_expiry_timestamp(ttl)
    created_at = get_ttl_expiry(ttl)

    entry = {
        "key": key,
        "value": value,
        "scope": scope,
        "ttl": ttl,
        "created_at": created_at,
        "expires_at": expires_at,
    }

    existing_key = f"{scope}:{key}"
    deduplicated = existing_key in store

    # Prune expired entries before storing
    prune_expired_entries(store)

    store[existing_key] = entry

    success = save_cache(workspace_path=workspace_path, cache_path=ctx_path, data=store)

    return {
        "success": success,
        "key": key,
        "scope": scope,
        "deduplicated": deduplicated,
        "expires_at": expires_at,
        "active_entries": len(store),
    }


async def get_context_fragment(
    workspace_path: str | Path,
    topic: str,
) -> dict[str, Any]:
    """
    Returns only what the AI needs for a specific topic.
    Avoids context bloat — no full file reads.

    Args:
        topic: The topic to search for (e.g., "database config", "auth setup").
        workspace_path: Project root path. If empty, falls back to CWD.

    Returns:
        { success, topic, fragment, sources }
    """
    fragment_parts: list[str] = []
    sources: list[str] = []
    topic_lower = topic.lower()

    # 1. Search context store for relevant entries
    ctx_path = get_context_path(workspace_path=workspace_path)
    stored = load_cache(workspace_path=workspace_path, cache_path=ctx_path)
    ctx_fragments, ctx_sources = search_context_store(stored or {}, topic_lower)
    fragment_parts.extend(ctx_fragments)
    sources.extend(ctx_sources)

    # 2. Search agent-recall for relevant patterns
    mem_fragments, mem_sources = search_memory_for_topic(workspace_path=workspace_path, topic=topic)
    fragment_parts.extend(mem_fragments)
    sources.extend(mem_sources)

    # 3. Search plan registry for relevant plan summaries
    reg_fragments, reg_sources = search_registry_for_topic(workspace_path=workspace_path, topic_lower=topic_lower)
    fragment_parts.extend(reg_fragments)
    sources.extend(reg_sources)

    return {
        "success": True,
        "topic": topic,
        "fragment": "\n".join(fragment_parts) if fragment_parts else f"No context found for topic: {topic}",
        "sources": sources,
        "entry_count": len(fragment_parts),
    }


# ── Helper ─────────────────────────────────────────────────────────────────


def get_context_path(workspace_path: str | Path, context_file: str | None = None) -> str:
    """Return the relative cache path for the context store JSON file."""
    ai_path = settings.get_ai_dir(workspace_path=workspace_path)
    memory_bank_path = settings.get_memory_bank_dir(workspace_path=workspace_path)
    file = context_file if isinstance(context_file, str) else _CONTEXT_STORE
    ctx_resolved_path = memory_bank_path / "memory" / file
    ctx_path = ctx_resolved_path.relative_to(ai_path)
    return str(ctx_path)

