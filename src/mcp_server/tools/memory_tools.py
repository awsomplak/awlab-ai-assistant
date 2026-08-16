"""
Memory tools — wrappers for agent-recall operations (used by tests).

The REGISTRY memory surface is ``mem_write`` / ``mem_search`` / ``mem_read`` /
``mem_remove`` (which use the agent-recall helpers directly). This module keeps
only the ``search_memory`` wrapper still referenced by the REGISTRY.
"""

from pathlib import Path
from typing import Any

from ..config import settings
from ..helpers import (
    fail_obj,
    ok_obj,
    re_rank_results,
    read_graph,
    search_nodes,
    store_target,
)


def _pattern_field(entity: dict, key: str) -> str:
    """Read a `key: value` observation line from a pattern entity."""
    for line in entity.get("observations") or entity.get("contents") or []:
        s = str(line)
        if s.startswith(key + ":"):
            return s.split(":", 1)[1].strip()
    return ""


def _project_stack(workspace_path: str | Path) -> str:
    """Detect the project stack (framework → language → 'any')."""
    try:
        from .context_tools.scanner import detect_framework

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


def _scope_patterns(
    entities: list[dict],
    workspace_path: str | Path,
    scope: str = "stack",
    context: str = "",
) -> list[dict]:
    """Filter pattern entities by retrieval scope + optional context.

    scope=stack (default): same-stack + ``any`` patterns (stack auto-detected).
    scope=project: only patterns learned in this project.
    scope=all: no stack/project filter (audit).
    context: keep patterns whose context or value contains the term.
    """
    if scope == "all":
        out = list(entities)
    elif scope == "project":
        slug = _project_slug(workspace_path)
        out = [e for e in entities if _pattern_field(e, "source_project") in ("", slug)]
    else:  # stack (default)
        stack = _project_stack(workspace_path)
        out = [e for e in entities if _pattern_field(e, "stack") in ("", "any", stack)]
    if context:
        c = context.lower()
        out = [e for e in out if c in _pattern_field(e, "context").lower() or c in _pattern_field(e, "value").lower()]
    return out


def _annotate_pattern(entity: dict) -> dict[str, Any]:
    """Return the pattern entity with its metadata parsed into ``meta``."""
    return {
        "name": entity.get("name"),
        "entityType": entity.get("entityType") or entity.get("type"),
        "meta": {
            "type": _pattern_field(entity, "type"),
            "value": _pattern_field(entity, "value"),
            "stack": _pattern_field(entity, "stack"),
            "context": _pattern_field(entity, "context"),
            "source_project": _pattern_field(entity, "source_project"),
            "confidence": _pattern_field(entity, "confidence"),
            "source": _pattern_field(entity, "source"),
            "timestamp": _pattern_field(entity, "timestamp"),
        },
    }


async def search_memory(
    workspace_path: str | Path,
    project_id: str | None = None,
    query: str = "",
    limit: int = 10,
    use_dense: bool = False,
    scope: str = "stack",
    entity_type: str = "",
    store: str = "project",
    context: str = "",
) -> dict[str, Any]:
    """Search agent-recall memory using hybrid search.

    Args:
        workspace_path: Project root.
        project_id: Optional project scope (per-project DB / slug isolation).
        query: Search query. When empty AND ``entity_type`` is set, lists ALL
            entities of that type deterministically (no text dependence).
        limit: Max results returned.
        use_dense: When True, re-rank candidates with BM25+dense (fastembed).
        scope: For ``store="patterns"`` — retrieval scope: ``stack`` (default,
            auto-detected from workspace), ``project`` (only this project's), or
            ``all`` (no filter). Accepted for the project store (compat, unused).
        entity_type: When set, only return entities whose ``entityType`` equals
            this value (exact match).
        store: "project" (default) → the project memory store; "patterns" → the
            dedicated cross-project user-patterns store.
        context: Optional area filter for patterns (matches pattern context/value).
    """
    patterns, family = store_target(store)
    # Baked-pattern injection (Phase 5): stack-scoped candidates ride alongside results.
    baked: list[dict[str, Any]] = []
    try:
        from ..helpers.baking import read_baked, scope_candidates

        baked = scope_candidates(
            read_baked(workspace_path).get("candidates") or [],
            _project_stack(workspace_path),
        )
    except Exception:  # noqa: BLE001 — injection is best-effort
        baked = []
    try:
        # Deterministic type listing: no query → read the graph and filter by type.
        if entity_type and not query:
            graph = read_graph(
                workspace_path=workspace_path, project_id=project_id, limit=1000, patterns=patterns, family=family
            )
            entities = graph.get("entities") or []
            result = [e for e in entities if (e.get("entityType") or "") == entity_type]
            if patterns:
                result = _scope_patterns(result, workspace_path, scope=scope, context=context)
                result = [_annotate_pattern(e) for e in result]
            return ok_obj(
                data=result[:limit],
                filtered_by="entity_type",
                store=store,
                scope=scope if patterns else None,
                baked_patterns=baked,
            )

        # Full-text (or hybrid) search over name + observations.
        candidate_limit = max(limit * 3, 30)
        result = search_nodes(
            workspace_path=workspace_path,
            project_id=project_id,
            query=query,
            limit=candidate_limit,
            patterns=patterns,
            family=family,
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
        if patterns:
            result = _scope_patterns(result, workspace_path, scope=scope, context=context)
            result = [_annotate_pattern(e) for e in result]
        return ok_obj(data=result[:limit], store=store, scope=scope if patterns else None, baked_patterns=baked)
    except Exception as e:
        return fail_obj(error=str(e))
