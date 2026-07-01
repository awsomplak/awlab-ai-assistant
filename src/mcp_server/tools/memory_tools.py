"""
Memory tools — wrappers for agent-recall operations (used by tests).
"""

from pathlib import Path
from typing import Any

from ..helpers import (
    add_observations,
    create_entities,
    search_nodes,
    ok_obj,
    fail_obj,
)


async def search_memory(
    workspace_path: str | Path,
    project_id: str | None = None,
    query: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Search agent-recall memory using hybrid search."""
    try:
        result = search_nodes(
            workspace_path=workspace_path,
            project_id=project_id,
            query=query,
            limit=limit,
        )
        return ok_obj(data=result)
    except Exception as e:
        return fail_obj(error=str(e))


async def store_memory(
    workspace_path: str | Path,
    entity_name: str,
    observation: str,
    pattern_type: str = "preference",
    project_id: str | None = None,
) -> dict[str, Any]:
    """Store a new observation in memory (creates entity if needed)."""
    try:
        existing = search_nodes(workspace_path=workspace_path, project_id=project_id, query=entity_name, limit=1)
        entity_exists = bool(existing and len(existing) > 0)

        if not entity_exists:
            entity = {"name": entity_name, "entityType": "pattern", "observations": []}
            create_entities(workspace_path=workspace_path, project_id=project_id, entities=[entity])

        add_observations(workspace_path=workspace_path, project_id=project_id, observations=[
            {"entityName": entity_name, "contents": [observation, f"type: {pattern_type}"]},
        ])
        return ok_obj(entity=entity_name, observation_added=True)
    except Exception as e:
        return fail_obj(error=str(e))


async def list_patterns(workspace_path: str | Path, project_id: str | None = None) -> dict[str, Any]:
    """List all stored patterns."""
    try:
        result = search_nodes(workspace_path=workspace_path, project_id=project_id, query="type: pattern")
        return ok_obj(patterns=result)
    except Exception as e:
        return fail_obj(error=str(e))
