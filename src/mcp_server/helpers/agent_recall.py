"""
Direct Python bridge to the agent-recall pip package (v0.4.0+).

Uses MCPBridge from agent_recall.mcp_bridge instead of subprocess CLI calls.
Supports two project isolation strategies:

1. **Per-project DB files** (backward-compatible with agent_recall_bridge.py):
   If ``project_id`` is provided to any function (or via the ``create_bridge``
   factory), a dedicated SQLite database ``memory_{sanitized_id}.db`` is used,
   with no scope isolation inside the bridge.

2. **Single-DB scope isolation** (default when no project_id is given):
   A single ``memory.db`` database is used, and memories are isolated via
   the scope chain ``["global", scope]``.
"""

import sqlite3
from pathlib import Path

from agent_recall import MCPBridge, MemoryConfig

from ..config import settings
from .workspace import resolve_db_path

# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_scope(
    workspace_path: str | Path,
    project_id: str | None = None
) -> str:
    """
    Return the project scope slug derived from project-id or directory name.

    Args:
        workspace_path: Absolute path to the project workspace root.
        project_id: Optional explicit project identifier. When provided, uses
                    a dedicated per-project database file with no scope chain
                    (backward-compatible with ``agent_recall_bridge``).
                    When None, uses scope isolation within a shared database.
    """
    if project_id:
        return project_id
    pid = settings.get_project_id(workspace_path)
    if pid:
        return pid

    return settings._resolve_workspace(workspace_path).name.lower().replace(" ", "_").replace("-", "_")


# ── SQLite optimisations ────────────────────────────────────────────────────


def _enable_wal_mode(db_path: str) -> None:
    """Enable WAL journal mode and busytimeout for concurrent access."""
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=3000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.close()
    except sqlite3.Error as e:
        from ..helpers.logger import logger
        logger.warning(f"Could not set WAL mode on {db_path}: {e}")


# ── Bridge lifecycle ─────────────────────────────────────────────────────────


def create_bridge(
    workspace_path: str | Path,
    project_id: str | None = None,
) -> MCPBridge:
    """
    Create an MCPBridge instance for a project.

    Args:
        workspace_path: The project root path.
        project_id: Optional explicit project identifier. When provided, uses
                    a dedicated per-project database file with no scope chain
                    (backward-compatible with ``agent_recall_bridge``).
                    When None, uses scope isolation within a shared database.

    Returns:
        A configured MCPBridge instance ready for use.
    """
    db_path = resolve_db_path(workspace_path=workspace_path, project_id=project_id)

    _enable_wal_mode(db_path)

    if project_id is not None:
        # Strategy A: per-project DB file (no scope isolation)
        return MCPBridge(
            db_path=db_path,
            default_scope="global",
            scope_chain=None,
            strict_scopes=False,
            scope_reads=True,
        )

    # Strategy B: single DB with scope isolation
    scope = _get_scope(workspace_path=workspace_path, project_id=project_id)
    config = MemoryConfig(yaml_path) if (yaml_path := settings.memory_yaml_path(workspace_path)) else None
    return MCPBridge(
        db_path=db_path,
        default_scope=scope,
        scope_chain=["global", scope],
        config=config,
    )


# ── Convenience wrappers ─────────────────────────────────────────────────────


def search_nodes(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Search entities by name or observation text. Returns list of entity dicts."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.search_nodes(query=query, limit=limit)


def open_nodes(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    names: list[str],
) -> list[dict]:
    """Retrieve full details for specific entities by name."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.open_nodes(names=names)


def read_graph(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    limit: int = 1000,
) -> dict:
    """Read the full knowledge graph as {entities: [...], relations: [...]}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.read_graph(limit=limit)


def create_entities(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    entities: list[dict],
) -> dict:
    """Create new entities with observations. Returns {created, updated, blocked}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.create_entities(entities=entities)


def add_observations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    observations: list[dict],
) -> dict:
    """Add observations to existing entities. Returns {added, blocked}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.add_observations(observations=observations)


def create_relations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    relations: list[dict],
) -> dict:
    """Create directed relations between entities. Returns {created, blocked}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.create_relations(relations=relations)


def delete_entities(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    names: list[str],
) -> dict:
    """Delete entities by name. Returns {deleted, blocked}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.delete_entities(names=names)


def delete_relations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    relations: list[dict],
) -> dict:
    """Archive relations. Returns {deleted, blocked}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.delete_relations(relations=relations)


def delete_observations(
    workspace_path: str | Path,
    project_id: str | None = None,
    *,
    deletions: list[dict],
) -> dict:
    """Archive observations by text match. Returns {deleted, blocked}."""
    with create_bridge(workspace_path=workspace_path, project_id=project_id) as bridge:
        return bridge.delete_observations(deletions=deletions)