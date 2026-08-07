---
name: awlab-mcp
description: Dispatch consolidated MCP actions via action_call(action=...).
---

# awlab-mcp — Action Reference

Call `action_call` with `action` + params. Server guarantees preconditions/pipeline;
responses include `executed`/`skipped` traces. Use `action_help(action)` for details.

## context
- **ctx_info** — Read project context: snapshot, memory-bank, scan, suggestions, or orchestration context.
  - Params: workspace_path, mode, filename, task_description, force_refresh, project_id, query
  - Example: `action_call(action="ctx_info")`

## graph
- **graph_build** — Build/update the code knowledge graph into .ai/codegraph/ (AST-only, no LLM).
  - Params: workspace_path, root, include_html, directed, project_id
  - Example: `action_call(action="graph_build", params={"workspace_path": "D:/Project/Foo"})`
- **graph_explain** — Explain a graph node (details + direct neighbours). Auto-freshens first.
  - Params: workspace_path, node, limit, root, project_id
  - Example: `action_call(action="graph_explain", params={"workspace_path": "D:/Project/Foo", "node": "registry"})`
- **graph_path** — Shortest path between two graph nodes. Auto-freshens first.
  - Params: workspace_path, a, b, root
  - Example: `action_call(action="graph_path", params={"workspace_path": "D:/Project/Foo", "a": "action_call", "b": "registry"})`
- **graph_query** — Search the code graph (labels / source files / types). Auto-freshens first.
  - Params: workspace_path, query, limit, root, project_id
  - Example: `action_call(action="graph_query", params={"workspace_path": "D:/Project/Foo", "query": "registry"})`
- **graph_status** — Report code-graph freshness (exists? stale? changed files).
  - Params: workspace_path, root
  - Example: `action_call(action="graph_status", params={"workspace_path": "D:/Project/Foo"})`

## memory
- **mem_read** — Read node details or the graph neighbourhood.
  - Params: workspace_path, project_id, node, limit
  - Example: `action_call(action="mem_read", params={"node": "MCPBridge"})`
- **mem_remove** — Archive entities or delete observations/relations.
  - Params: workspace_path, project_id, names, deletions, relations
  - Example: `action_call(action="mem_remove", params={"names": ["stale-entity"]})`
- **mem_search** — Hybrid BM25+dense search over memory (optionally by entity type).
  - Params: workspace_path, query, project_id, limit, use_dense, entity_type
  - Example: `action_call(action="mem_search", params={"query": "registry schema"})`
- **mem_write** — Create/tag entities, add observations, or relate entities.
  - Params: workspace_path, project_id, entities, observations, relations
  - Example: `action_call(action="mem_write", params={"observations": [{"entityName": "A", "contents": ["x"]}]})`

## plan
- **plan_status** — Read plan/registry status: active plan, next task, completeness, phase gate.
  - Params: workspace_path, project_id, plan_uuid, phase, format
  - Example: `action_call(action="plan_status", params={"phase": 2})`
- **plan_update** — Mutate plan/registry: switch active plan, mark phase complete, resolve deferred tasks.
  - Params: workspace_path, project_id, mode, plan_uuid, phase_number
  - Example: `action_call(action="plan_update", params={"mode": "switch", "plan_uuid": "mcptool1"})`

## task
- **task_read** — Read a plan's tasks.md as structured/raw/minimal JSON.
  - Params: workspace_path, plan_uuid, format
  - Example: `action_call(action="task_read", params={"plan_uuid": "mcptool1", "format": "structured"})`
- **task_update** — Create or update tasks.md / tasks (multi-level paths, atomic).
  - Params: workspace_path, plan_uuid, project_id, content, updates, format, phases
  - Example: `action_call(action="task_update", params={"updates": [{"task_path": "1.2", "new_status": "[x]"}]})`

## util
- **util_info** — Server version / project metadata (or mermaid generation).
  - Params: mode, phases, dependencies
  - Example: `action_call(action="util_info")`

## workflow
- **wf** — List or execute a workflow.
  - Params: workspace_path, action, workflow_name, params
  - Example: `action_call(action="wf", params={"action": "execute", "workflow_name": "scan-project"})`
