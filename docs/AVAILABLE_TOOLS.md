# Available MCP Tools

> The MCP surface is **2 tools**: `action_call` (dispatcher) + `action_help` (help).
> Everything is driven by a single [`REGISTRY`](./REGISTRY_SCHEMA.md) — no drift.

---

## Server Architecture

One consolidated executable — the `REGISTRY` routes to all actions:

| Binary | Server | Exposed Tools |
|--------|--------|---------------|
| `awlab-mcp.exe` | `awlab-mcp` | `action_call`, `action_help` |

---

## Exposed Tools

### `action_call(action, params=None)`

Dispatch an MCP action. The server runs preconditions/pipeline automatically; responses include `executed`/`skipped` traces.

```
action_call(action="task_read", params={"plan_uuid": "mcptool1", "format": "structured"})
```

### `action_help(action=None)`

Get per-action usage (params, defaults, example, preconditions, pipeline) or a grouped overview when called with no args.

---

## Actions (group -> name)

### context

| Action | Summary |
|--------|---------|
| `ctx_info` | Read project context: snapshot, memory-bank, scan, suggestions, or orchestration context. |

### memory

| Action | Summary |
|--------|---------|
| `mem_read` | Read node details or the graph neighbourhood. |
| `mem_remove` | Archive entities or delete observations/relations. |
| `mem_search` | Hybrid BM25+dense search over memory (optionally by entity type). |
| `mem_write` | Create/tag entities, add observations, or relate entities. |

### plan

| Action | Summary |
|--------|---------|
| `plan_status` | Read plan/registry status: active plan, next task, completeness, phase gate. |
| `plan_update` | Mutate plan/registry: switch active plan, mark phase complete, resolve deferred tasks. |

### graph

| Action | Summary |
|--------|---------|
| `graph_build` | Build/update the code knowledge graph into .ai/codegraph/ (AST-only, no LLM). |
| `graph_status` | Report code-graph freshness (exists? stale? changed files). |
| `graph_query` | Search the code graph (labels / source files / types). Auto-freshens first. |
| `graph_path` | Shortest path between two graph nodes. Auto-freshens first. |
| `graph_explain` | Explain a graph node (details + direct neighbours). Auto-freshens first. |

#### Graph freshness contract

Every graph read (`graph_query`, `graph_path`, `graph_explain`) returns these
metadata fields so the agent can always tell whether the data is current and
whether a rebuild is in flight:

| Field | Type | Meaning |
|-------|------|---------|
| `graph_fresh` | `bool` | Whether the served graph was fresh at read time (source unchanged since the last build). |
| `graph_exists` | `bool` | Whether a graph exists yet (`false` on a first-ever read). |
| `graph_rebuilding` | `bool` | `true` when a heavy rebuild is running in the background (the read may have served slightly stale data). |
| `graph_built_at` | `str` | ISO timestamp of the last successful build. |

**Freshness behavior:**
- A stale graph with **few changed files** → rebuilt **synchronously** before the read (results are accurate).
- A stale graph with **many changed files (≥ 20) or a first build** → rebuilt in a **background thread**; the read returns immediately and may serve the previous graph. When `graph_rebuilding: true`, wait a moment and re-read (the next read is fresh).
- `graph_build` (explicit) during an in-flight background rebuild **coalesces** — it returns `rebuilding: true` instead of starting a duplicate build.

### task

| Action | Summary |
|--------|---------|
| `task_read` | Read a plan's tasks.md as structured/raw/minimal JSON. |
| `task_update` | Create or update tasks.md / tasks (multi-level paths, atomic). |

### util

| Action | Summary |
|--------|---------|
| `util_info` | Server version / project metadata (or mermaid generation). |

### workflow

| Action | Summary |
|--------|---------|
| `wf` | List or execute a workflow. |
