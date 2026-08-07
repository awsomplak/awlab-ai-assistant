"""
Memory tools — wrappers for agent-recall operations (used by tests).

The REGISTRY memory surface is ``mem_write`` / ``mem_search`` / ``mem_read`` /
``mem_remove`` (which use the agent-recall helpers directly). This module keeps
only the ``search_memory`` wrapper still referenced by the REGISTRY.
"""

from pathlib import Path
from typing import Any

from ..helpers import (
    fail_obj,
    ok_obj,
    re_rank_results,
    read_graph,
    search_nodes,
)


async def search_memory(
    workspace_path: str | Path,
    project_id: str | None = None,
    query: str = "",
    limit: int = 10,
    use_dense: bool = False,
    scope: str = "project",
    entity_type: str = "",
) -> dict[str, Any]:
    """Search agent-recall memory using hybrid search.

    Args:
        workspace_path: Project root.
        project_id: Optional project scope (per-project DB / slug isolation).
        query: Search query. When empty AND ``entity_type`` is set, lists ALL
            entities of that type deterministically (no text dependence).
        limit: Max results returned.
        use_dense: When True, re-rank candidates with BM25+dense (fastembed);
            falls back to BM25-only when fastembed is unavailable.
        scope: Declared for REGISTRY compat (project|user|conversation). Actual
            scoping is driven by ``project_id`` + ``workspace_path`` in
            agent-recall; this parameter is accepted but does not filter further.
        entity_type: When set, only return entities whose ``entityType`` equals
            this value (exact match). This is the CORRECT way to list patterns
            (``entity_type="pattern"``) — the old ``query="type: pattern"`` text
            hack was unreliable (FTS matches observations, not the type field).
    """
    try:
        # Deterministic type listing: no query → read the graph and filter by type.
        if entity_type and not query:
            graph = read_graph(workspace_path=workspace_path, project_id=project_id, limit=1000)
            entities = graph.get("entities") or []
            result = [e for e in entities if (e.get("entityType") or "") == entity_type]
            return ok_obj(data=result[:limit], filtered_by="entity_type")

        # Full-text (or hybrid) search over name + observations.
        candidate_limit = max(limit * 3, 30)
        result = search_nodes(
            workspace_path=workspace_path,
            project_id=project_id,
            query=query,
            limit=candidate_limit,
        )
        if entity_type:
            result = [e for e in result if (e.get("entityType") or "") == entity_type]
        if use_dense and result:
            texts = [
                " ".join(
                    [str(e.get("name", "")), *(str(o) for o in (e.get("observations") or e.get("contents") or []))]
                )
                for e in result
            ]
            ids = [str(e.get("name", "") or i) for i, e in enumerate(result)]
            result = re_rank_results(query, texts, ids, result)
        return ok_obj(data=result[:limit])
    except Exception as e:
        return fail_obj(error=str(e))
