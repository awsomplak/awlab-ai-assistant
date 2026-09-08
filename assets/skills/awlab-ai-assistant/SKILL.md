---
name: AWLab-AI-Assistant
description: Dispatch consolidated MCP actions via action_call(action=...). Two tools only: action_call + action_help. Always pass workspace_path; params is a single nested JSON object — never flatten at the top level.
---

# AWLab-AI-Assistant — Action Reference

## ⚠️ Read this first (most first-contact failures happen here)

The MCP server exposes **exactly two tools** to you:
  1. `action_call(action, params)` — the dispatcher.
  2. `action_help(action=None)` — per-action usage. **Call it as a separate
     tool call, NOT via `action_call(action='action_help', ...)`**.

**Call shape (strict):**
```
action_call(
  action='ctx_info',
  params={'workspace_path': '/abs/project/root', 'mode': 'context'},
)
```
❌ NEVER flatten params at the top level:
```
action_call(action='ctx_info', mode='context', workspace_path='...')  # WRONG
```
The tool only knows two top-level keys: `action` and `params`. Anything else is dropped.

**`workspace_path` is required** by every file-touching action (plan, task, memory,
graph, ctx_info). Pass the absolute project root. Omitting it is the #1 first-call error.

**Type strictness:** integers are `int` (not `'10'`), booleans are `bool` (not `0`/`1`),
arrays are `list`, enums must match exactly. The server validates before running.

**First-response workflow:**
  1. `action_call(action='project_id', params={'workspace_path': <root>})` — isolation.
  2. `action_call(action='ctx_info', params={'workspace_path': <root>, 'mode': 'context'})`
     — one server-owned call: plan + next task + code + memory + patterns + context_md.
  3. Then the action you actually wanted.

Server guarantees preconditions/pipeline run automatically; responses include
`executed` (steps that did work) and `skipped` (idempotent gates already satisfied).

## Actions (group → name)

### context
- **ctx_info** — Read project context: snapshot, memory-bank, scan, suggestions, or orchestration context.
  - Params: workspace_path, mode, filename, task_description, force_refresh, project_id, query
  - Example: `action_call(action="ctx_info")`
- **family_config** — Create / update / remove project-family entries in project-families.json.
  - Params: workspace_path, op, slug, name, members, path, project_id, replace_members
  - Example: `action_call(action="family_config", params={"op": "create", "slug": "my_app", "name": "My App", "members": [{"path": "D:/Project/frontend", "project_id": "frontend"}, {"path": "D:/Project/backend", "project_id": "backend"}]})`
- **family_info** — Read-only project-family discovery (global project-families.json).
  - Params: workspace_path, slug, project_id
  - Example: `action_call(action="family_info", params={"workspace_path": "D:/Project/Foo"})`
- **project_id** — Check the project-id; auto-create it if missing (idempotent).
  - Params: workspace_path, project_id, force_regenerate
  - Example: `action_call(action="project_id", params={"workspace_path": "..."})`

### graph
- **graph_build** — Build/update the code knowledge graph into .ai/codegraph/ (AST-only, no LLM).
  - Params: workspace_path, root, family, include_html, node_limit, max_files, chunk_size, background, force, directed, project_id
  - Example: `action_call(action="graph_build", params={"workspace_path": "D:/Project/Foo"})`
- **graph_explain** — Explain a graph node (details + direct neighbours). Auto-freshens first.
  - Params: workspace_path, node, limit, depth, root, family, project_id
  - Example: `action_call(action="graph_explain", params={"workspace_path": "D:/Project/Foo", "node": "registry"})`
- **graph_path** — Shortest path between two graph nodes. Auto-freshens first.
  - Params: workspace_path, a, b, from_node, to_node, root, family
  - Example: `action_call(action="graph_path", params={"workspace_path": "D:/Project/Foo", "a": "action_call", "b": "registry"})`
- **graph_query** — Search the code graph (labels / source files / types). Auto-freshens first.
  - Params: workspace_path, query, limit, kind, group_by_file, root, family, project_id
  - Example: `action_call(action="graph_query", params={"workspace_path": "D:/Project/Foo", "query": "registry"})`
- **graph_status** — Report code-graph freshness (exists? stale? changed files).
  - Params: workspace_path, root, family
  - Example: `action_call(action="graph_status", params={"workspace_path": "D:/Project/Foo"})`

### memory
- **mem_dedupe** — Merge same-named memory entities (keep data-bearing, archive dupes).
  - Params: workspace_path, project_id, name, dry_run, store
  - Example: `action_call(action="mem_dedupe", params={"name": "Bus Service"})`
- **mem_list_entities** — List all memory entities (name/type/obs count) for auditing.
  - Params: workspace_path, project_id, limit, store
  - Example: `action_call(action="mem_list_entities", params={"limit": 200})`
- **mem_observe** — Record user-pattern evidence into the observation store (baking input).
  - Params: workspace_path, project_id, observations, raw_text, stack
  - Example: `action_call(action="mem_observe", params={"observations": [{"signature": "cmd_pnpm_install", "value": "pnpm install", "source": "behavioral", "stack": "nodejs"}]})`
- **mem_read** — Read node details or the graph neighbourhood.
  - Params: workspace_path, project_id, node, limit, store
  - Example: `action_call(action="mem_read", params={"node": "MCPBridge"})`
- **mem_remove** — Archive entities or delete observations/relations (type-safe).
  - Params: workspace_path, project_id, names, entities, deletions, relations, store
  - Example: `action_call(action="mem_remove", params={"entities": [{"name": "X", "entityType": "concept"}]})`
- **mem_replay** — Replay the offline cache (pending.jsonl): re-apply queued mutations.
  - Params: workspace_path, dry_run
  - Example: `action_call(action="mem_replay", params={"dry_run": True})`
- **mem_search** — Hybrid BM25+dense search over memory (optionally by entity type).
  - Params: workspace_path, query, project_id, limit, use_dense, entity_type, scope, context, store
  - Example: `action_call(action="mem_search", params={"query": "registry schema"})`
- **mem_write** — Create/tag entities, add observations, or relate entities.
  - Params: workspace_path, project_id, entities, observations, relations, raw_text, store
  - Example: `action_call(action="mem_write", params={"observations": [{"entityName": "A", "contents": ["x"]}]})`

### plan
- **plan_create** — Create a new plan: auto-generates UUID, scaffolds files, updates registry.
  - Params: workspace_path, project_id, summary
  - Example: `action_call(action="plan_create", params={"summary": "Migrate database to PostgreSQL"})`
- **plan_doc** — Read / create / update / delete a plan's plan.md, notes.md, or walkthrough.md directly.
  - Params: workspace_path, plan_uuid, project_id, doc, mode, content
  - Example: `action_call(action="plan_doc", params={"plan_uuid": "ab12cd34", "doc": "plan", "mode": "write", "content": "# Plan\n\n## Overview\n"})`
- **plan_status** — Read plan/registry status: active plan, next task, completeness, phase gate.
  - Params: workspace_path, project_id, plan_uuid, phase, format
  - Example: `action_call(action="plan_status", params={"phase": 2})`
- **plan_update** — Mutate plan/registry: switch, mark phase complete, resolve deferred, run retrospective.
  - Params: workspace_path, project_id, mode, plan_uuid, phase_number
  - Example: `action_call(action="plan_update", params={"mode": "mark_phase", "plan_uuid": "mcptool1", "phase_number": 2})`
- **reg_update** — Single registry.md CRUD: create / update status / delete a plan row.
  - Params: workspace_path, project_id, type, summary, uuid, status, confirmed
  - Example: `action_call(action="reg_update", params={"type": "create", "summary": "New plan"})`

### task
- **task_read** — Read a plan's tasks.md as structured/raw/minimal JSON.
  - Params: workspace_path, plan_uuid, format
  - Example: `action_call(action="task_read", params={"plan_uuid": "mcptool1", "format": "structured"})`
- **task_update** — Create or update tasks.md / tasks (multi-level paths, atomic).
  - Params: workspace_path, plan_uuid, project_id, content, updates, format, phases
  - Example: `action_call(action="task_update", params={"updates": [{"task_path": "1.2", "new_status": "[x]"}]})`

### util
- **util_info** — Server version / project metadata (or mermaid generation).
  - Params: mode, phases, dependencies
  - Example: `action_call(action="util_info")`

### workflow
- **wf** — List or execute a workflow.
  - Params: workspace_path, action, workflow_name, params, workflows_dir
  - Example: `action_call(action="wf", params={"action": "execute", "workflow_name": "scan-project"})`

## Codebase navigation — graph first (call BEFORE reading source files)

To locate a symbol / method / class / caller or trace how code connects, use the 
code knowledge graph (AST-accurate, auto-freshens):
- `graph_query` — find node(s) by symbol name; a missing graph is auto-built 
  (returns freshness metadata).
- `graph_explain` — a hit's declaration + direct neighbours/callers.
- `graph_path` — shortest dependency path between two symbols.
- Only then read the 1-3 most relevant files (bounded by the 5-file turn budget).
- Fall back to grep/source scan ONLY for exact literal text (strings/comments/
  config values) or when a graph query dead-ends.

## Cross-cutting: Project Family Memory (shared stores + merged graph)

Project families group correlated repos (backend + frontend, etc.) so they share one
merged code graph and one shared family memory store.

- MEMORY (`store`): `store="family_<slug>"` targets the SHARED family memory, isolated
  per family. Default `store="project"` stays in this project's own store;
  `store="patterns"` targets the cross-project user-patterns store. Only one at a time.
- GRAPH (`family`): pass `family="<slug>"` to the graph_* actions to operate on the
  MERGED cross-project graph — nodes are tagged `project_id::` so you can tell members apart.
- The slug MUST match a key in the family config: `~/.awlab-id/agent-memory/project-families.json`
  (v2 shape: {slug: {name, members: [{path, project_id}]}}). It is never invented.
  If this workspace belongs to a family, `ctx_info` reports the resolved slug; otherwise
  read that config file directly (or ask the user which family to use).
- Example: `action_call(action="mem_search", params={"workspace_path": "...", "store":
  "family_my_app", "query": "login flow"})`.
- Full setup guide: docs/en/PROJECT_FAMILIES.md (Indonesian: docs/id/PROJECT_FAMILIES.md).

Actions whose help output ends with a `## Family Memory` section are family-aware (store=`family_<slug>` or family=`<slug>`); call action_help on them for the exact syntax.
