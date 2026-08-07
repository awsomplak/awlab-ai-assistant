"""
Memory search helpers — query agent-recall for pattern observations.

Provides ``query_agent_recall_for_patterns`` (used by ``get_context_snapshot``),
which searches the agent-recall knowledge graph for ``pattern``-typed entities.
The legacy TTL/context-store/fragment helpers were removed with the old
``store_context`` / ``get_context_fragment`` tools.
"""

from pathlib import Path
from typing import Any

from ...helpers import (
    read_graph as _read_graph,
)


def query_agent_recall_for_patterns(
    workspace_path: str | Path = "", project_id: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Query agent-recall for pattern entities (deterministic).

    Reads the knowledge graph and filters by the exact ``entityType == 'pattern'``
    field. This replaces the old text search (``query="pattern"`` + substring
    match), which was unreliable — FTS matches observations, so non-pattern
    entities that merely mention "pattern" could be returned.
    """
    try:
        graph = _read_graph(workspace_path=workspace_path, project_id=project_id, limit=1000)
        entities = graph.get("entities") or []
        patterns = []
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if (entity.get("entityType") or entity.get("type", "")) != "pattern":
                continue
            observations = entity.get("observations") or entity.get("contents") or []
            if isinstance(observations, str):
                observations = [observations]
            patterns.append(
                {
                    "name": entity.get("name", ""),
                    "observations": observations[:5],
                }
            )
            if len(patterns) >= limit:
                break
        return patterns
    except Exception:
        return []
