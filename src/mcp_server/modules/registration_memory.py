"""
Tool registration for awlab-memory server — all memory/knowledge-graph tools.

Creates its own FastMCP("awlab-memory") instance and registers only
memory-related tools (mem_*, ctx_store, ctx_get_fragment).
    """

import json
from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP
from ..helpers.logger import logger

# ── Own FastMCP instance ─────────────────────────────────────────────────

mcp = FastMCP("awlab-memory")


# ── Import business-logic implementations ─────────────────────────────────

from ..helpers import (
    add_observations as _recall_add_observations,
    create_entities as _recall_create_entities,
    create_relations as _recall_create_relations,
    open_nodes as _recall_open_nodes,
    delete_entities as _recall_delete_entities,
    delete_observations as _recall_delete_observations,
    delete_relations as _recall_delete_relations,
    read_graph as _recall_read_graph,
    search_nodes as _recall_search_nodes,
    invalid_scope as _invalid_scope,
    ok_json as _ok,
    fail_json as _fail,
)
from ..tools.context_tools import (
    store_context as _store_context,
    get_context_fragment as _get_context_fragment,
)
from ..helpers.hybrid_search import re_rank_results


# ══════════════════════════════════════════════════════════════════════════
# ── Memory Search ───────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="mem_search")
async def search_memory(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    query: Annotated[str, Field(description="Search query string")],
    project_id: Annotated[str | None, Field(description="Optional project ID to scope the query")] = None,
    scope: Annotated[str, Field(description="Scope: project, user, or conversation")] = "project",
    limit: Annotated[int, Field(description="Max results to return")] = 10,
    use_dense: Annotated[bool, Field(description="Enable BM25 + dense re-ranking")] = False,
) -> str:
    """
    Search memory.
    Search memory with hybrid BM25+dense ranking."""
    if scope not in ("project", "user", "conversation"):
        return _invalid_scope(scope)
    try:
        result = _recall_search_nodes(
            workspace_path=workspace_path,
            project_id=project_id,
            query=query,
            limit=limit,
        )
        if use_dense and isinstance(result, list) and result:
            try:
                texts: list[str] = []
                ids: list[str] = []
                for ent in result:
                    name = ent.get("name", "") or ent.get("entity", "")
                    obs = ent.get("observations") or ent.get("contents") or []
                    if isinstance(obs, str):
                        obs = [obs]
                    combined = name + " " + " ".join(str(o) for o in obs[:5] if o)
                    texts.append(combined.strip())
                    ids.append(name or str(len(ids)))
                if texts:
                    ranked = re_rank_results(query, texts, ids, result)
                    result = ranked[:limit]
            except Exception:
                pass  # fall back to original order
        return _ok(data=result)
    except Exception as e:
        logger.tool("search_memory").error(f"search_memory failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_store")
async def store_memory(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], entity_name: Annotated[str, Field(description="Name of the entity")], observation: Annotated[str, Field(description="Observation text to attach")], pattern_type: Annotated[str, Field(description="Pattern type: preference, convention, workflow, anti_pattern")] = "preference") -> str:
    """
    Store observation.
    Store an observation (creates entity if needed)."""
    try:
        existing = _recall_search_nodes(workspace_path=workspace_path, query=entity_name, limit=1)
        entity_exists = bool(existing and len(existing) > 0)
        if not entity_exists:
            entity = {"name": entity_name, "entityType": "pattern", "observations": []}
            _recall_create_entities(workspace_path=workspace_path, entities=[entity])
        type_observation = f"type: {pattern_type}"
        _recall_add_observations(workspace_path=workspace_path, observations=[
            {"entityName": entity_name, "contents": [observation, type_observation]},
        ])
        return _ok(entity=entity_name, observation_added=True)
    except Exception as e:
        logger.tool("store_memory").error(f"store_memory failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_list_patterns")
async def list_patterns(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")]) -> str:
    """
    List patterns.
    List all stored patterns in memory."""
    try:
        result = _recall_search_nodes(workspace_path=workspace_path, query="type: pattern")
        return _ok(patterns=result)
    except Exception as e:
        logger.tool("list_patterns").error(f"list_patterns failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_create_entities")
async def create_entities(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], entities: Annotated[list[dict], Field(description="List of {name, entityType, observations} dicts")]) -> str:
    """
    Create entities.
    Create new entities in memory."""
    try:
        result = _recall_create_entities(workspace_path=workspace_path, entities=entities)
        return _ok(result=result)
    except Exception as e:
        logger.tool("create_entities").error(f"create_entities failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_tag_entity")
async def tag_entity(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], observations: Annotated[list[dict], Field(description="List of {entityName, contents} dicts")]) -> str:
    """
    Tag entity with context.
    Tag an entity with additional context labels."""
    try:
        result = _recall_add_observations(workspace_path=workspace_path, observations=observations)
        return _ok(result=result)
    except Exception as e:
        logger.tool("add_observations").error(f"add_observations failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_relate")
async def relate(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], relations: Annotated[list[dict], Field(description="List of {from, to, relationType} dicts")]) -> str:
    """
    Create semantic relation.
    Declare a semantic association between two entities."""
    try:
        result = _recall_create_relations(workspace_path=workspace_path, relations=relations)
        return _ok(result=result)
    except Exception as e:
        logger.tool("create_relations").error(f"create_relations failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_fetch_node_details")
async def fetch_node_details(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], names: Annotated[list[str], Field(description="List of entity names")]) -> str:
    """
    Query node attributes.
    Query attributes of specific graph nodes."""
    try:
        result = _recall_open_nodes(workspace_path=workspace_path, names=names)
        return _ok(results=result)
    except Exception as e:
        logger.tool("open_nodes").error(f"open_nodes failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_read_graph")
async def read_graph(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], limit: Annotated[int, Field(description="Max results to return")] = 50) -> str:
    """
    Read knowledge graph.
    Read the knowledge graph."""
    try:
        result = _recall_read_graph(workspace_path=workspace_path, limit=limit)
        return _ok(results=result)
    except Exception as e:
        logger.tool("read_graph").error(f"read_graph failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_archive_entities")
async def archive_entities(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], names: Annotated[list[str], Field(description="List of entity names")]) -> str:
    """
    Archive entities.
    Move entities to a historical archive state."""
    try:
        result = _recall_delete_entities(workspace_path=workspace_path, names=names)
        return _ok(result=result)
    except Exception as e:
        logger.tool("delete_entities").error(f"delete_entities failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_delete_observations")
async def delete_observations(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], deletions: Annotated[list[dict], Field(description="List of {entityName, observations} dicts")]) -> str:
    """
    Delete observations.
    Remove specific observations from entities."""
    try:
        result = _recall_delete_observations(workspace_path=workspace_path, deletions=deletions)
        return _ok(result=result)
    except Exception as e:
        logger.tool("delete_observations").error(f"delete_observations failed: {e}")
        return _fail(str(e))


@mcp.tool(name="mem_delete_relations")
async def delete_relations(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], relations: Annotated[list[dict], Field(description="List of {from, to, relationType} dicts")], cascade: Annotated[bool, Field(description="Also delete orphaned relations")] = False) -> str:
    """
    Delete relations.
    Remove relations between entities."""
    try:
        result = _recall_delete_relations(workspace_path=workspace_path, relations=relations)
        deleted = result.get("deleted", [])
        cascade_deleted: list[dict] = []

        if cascade and relations:
            entity_names: set[str] = set()
            for r in relations:
                if isinstance(r, dict):
                    if "from" in r:
                        entity_names.add(r["from"])
                    if "to" in r:
                        entity_names.add(r["to"])
            if entity_names:
                graph = _recall_read_graph(workspace_path=workspace_path, limit=5000)
                all_relations = graph.get("relations", []) if isinstance(graph, dict) else []
                orphan_relations = [
                    r for r in all_relations
                    if isinstance(r, dict)
                    and (r.get("from") in entity_names or r.get("to") in entity_names)
                ]
                original_keys: set[str] = set()
                for r in relations:
                    if isinstance(r, dict):
                        key = f"{r.get('from','')}|{r.get('to','')}|{r.get('relationType','')}"
                        original_keys.add(key)
                cascade_targets = [
                    r for r in orphan_relations
                    if f"{r.get('from','')}|{r.get('to','')}|{r.get('relationType','')}" not in original_keys
                ]
                if cascade_targets:
                    cascade_result = _recall_delete_relations(workspace_path=workspace_path, relations=cascade_targets)
                    cascade_deleted = cascade_result.get("deleted", [])
        return _ok(result=result, cascade_deleted=cascade_deleted)
    except Exception as e:
        logger.tool("delete_relations").error(f"delete_relations failed: {e}")
        return _fail(str(e))


# ══════════════════════════════════════════════════════════════════════════
# ── Context Storage (memory-adjacent) ──────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="ctx_store")
async def store_context(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], key: Annotated[str, Field(description="Context fragment key")], value: Annotated[str, Field(description="Context value to store")], scope: Annotated[str, Field(description="Scope: project, user, or conversation")] = "project", ttl: Annotated[int, Field(description="TTL in seconds (-1 = no expiry)")] = 3600) -> str:
    """
    Store context with TTL.
    Store a context fragment with TTL-based expiry."""
    if scope not in ("project", "user", "conversation"):
        return _invalid_scope(scope)
    result = await _store_context(workspace_path, key, value, scope, ttl)
    return json.dumps(result)


@mcp.tool(name="ctx_get_fragment")
async def get_context_fragment(workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")], topic: Annotated[str, Field(description="Topic to search context for")]) -> str:
    """
    Get context fragment.
    Retrieve context for a topic without file reads."""
    result = await _get_context_fragment(workspace_path, topic)
    return json.dumps(result)
