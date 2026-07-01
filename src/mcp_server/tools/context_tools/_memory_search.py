"""
Memory search helpers — query agent-recall and cache for context fragments.

Extracted from context.py for Task 30 refactoring.
"""

from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any
from ...helpers import (
    load_registry,
    search_nodes as _search_nodes,
)


def get_ttl_expiry(ttl: int) -> str:
    """Return an ISO-formatted expiry timestamp for a given TTL in seconds."""
    return (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()


def prune_expired_entries(store: dict[str, Any]) -> list[str]:
    """Remove expired entries from store and return deleted keys."""
    now = datetime.now(UTC)
    keys_to_delete: list[str] = []
    for k, v in store.items():
        exp = v.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if exp_dt < now.replace(tzinfo=exp_dt.tzinfo if exp_dt.tzinfo else None):
                    keys_to_delete.append(k)
            except (ValueError, TypeError):
                pass
    for k in keys_to_delete:
        del store[k]
    return keys_to_delete


def build_expiry_timestamp(ttl: int) -> str | None:
    """Return an ISO-formatted expiry timestamp or None for no expiry."""
    if ttl > 0:
        return get_ttl_expiry(ttl)
    return None


def is_expired(entry: dict[str, Any]) -> bool:
    """Check if a store entry is expired."""
    exp = entry.get("expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        now = datetime.now(UTC).replace(tzinfo=exp_dt.tzinfo if exp_dt.tzinfo else None)
        return exp_dt < now
    except (ValueError, TypeError):
        return False


def query_agent_recall_for_patterns(
    workspace_path: str | Path = "",
    project_id: str | None = None,
    limit: int = 5
) -> list[dict[str, Any]]:
    """Query agent-recall for recent pattern observations."""
    try:
        results = _search_nodes(
            workspace_path=workspace_path,
            project_id=project_id,
            query="pattern",
            limit=limit * 2
        )
        if not isinstance(results, list):
            return []
        patterns = []
        for entity in results:
            if not isinstance(entity, dict):
                continue
            etype = entity.get("entityType") or entity.get("type", "")
            entity_name = entity.get("name", "")
            if "pattern" in etype.lower() or "pattern" in entity_name.lower():
                observations = entity.get("observations") or entity.get("contents") or []
                if isinstance(observations, str):
                    observations = [observations]
                patterns.append({
                    "name": entity_name,
                    "observations": observations[:5],
                })
            if len(patterns) >= limit:
                break
        return patterns
    except Exception:
        return []


def search_context_store(store: dict[str, Any], topic_lower: str) -> tuple[list[str], list[str]]:
    """Search context store entries matching a topic. Returns (fragments, sources)."""
    fragments: list[str] = []
    sources: list[str] = []
    for key, entry in store.items():
        if is_expired(entry):
            continue
        if topic_lower in key.lower() or topic_lower in entry.get("value", "").lower():
            fragments.append(f"[context:{entry['scope']}] {entry['key']} = {entry['value']}")
            sources.append(f"context_store:{key}")
    return fragments, sources


def search_memory_for_topic(
    workspace_path: str | Path = "",
    project_id: str | None = None,
    *,
    topic: str,
    limit: int = 5,
) -> tuple[list[str], list[str]]:
    """Search agent-recall for entities matching the topic. Returns (fragments, sources)."""
    fragments: list[str] = []
    sources: list[str] = []
    try:
        memory_results = _search_nodes(
            workspace_path=workspace_path,
            project_id=project_id,
            query=topic,
            limit=limit,
        )
        if isinstance(memory_results, list):
            for entity in memory_results:
                if not isinstance(entity, dict):
                    continue
                name = entity.get("name", "")
                observations = entity.get("observations") or entity.get("contents") or []
                if isinstance(observations, str):
                    observations = [observations]
                for obs in observations[:2]:
                    if isinstance(obs, str):
                        fragments.append(f"[memory:{entity.get('entityType', 'entity')}] {name}: {obs[:200]}")
                        sources.append(f"memory:{name}")
    except Exception:
        pass
    return fragments, sources


def search_registry_for_topic(
    workspace_path: str | Path = "",
    topic_lower: str = "",
) -> tuple[list[str], list[str]]:
    """Search plan registry for entries matching the topic. Returns (fragments, sources)."""
    fragments: list[str] = []
    sources: list[str] = []
    registry = load_registry(workspace_path=workspace_path)
    for section_key in ["active", "paused", "completed"]:
        entry = registry.get(section_key)
        if isinstance(entry, dict) and entry.get("summary"):
            summary = str(entry.get("summary", ""))
            if topic_lower in summary.lower():
                fragments.append(f"[plan:{section_key}] {entry.get('uuid', '')}: {summary}")
                sources.append(f"plan:{section_key}")
        elif isinstance(entry, list):
            for item in entry:
                if isinstance(item, dict) and item.get("summary"):
                    summary = str(item.get("summary", ""))
                    if topic_lower in summary.lower():
                        fragments.append(f"[plan:{section_key}] {item.get('uuid', '')}: {summary}")
                        sources.append(f"plan:{section_key}/{item.get('uuid', '')}")
    return fragments, sources