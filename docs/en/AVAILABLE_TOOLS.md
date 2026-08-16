# Available MCP Tools

> [🏠 README](../../README.md) · [📚 Docs](../../README.md#documentation) · **Available MCP Tools**

> The MCP surface is **2 tools**: `action_call` (dispatcher) + `action_help` (help), routing **23 actions**.

**In this page:**

- [Server architecture](#server-architecture)
- [Exposed tools](#exposed-tools)
- [Actions (group → name)](#actions-group---name)
- [Pattern baking & delivery](#pattern-baking--delivery)
- [Offline cache (`pending.jsonl`)](#offline-cache-pendingjsonl)
- [Project families](#project-families)

---

## Server Architecture

One consolidated executable — the `REGISTRY` routes to all actions:

| Binary | Server | Exposed Tools |
|--------|--------|---------------|
| `awlab-ai-assistant.exe` | `awlab-ai-assistant` | `action_call`, `action_help` |

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
| `project_id` | Check the project-id; auto-create it if missing (idempotent). Call this on first response, before any `mem_*`/plan op, so memory isolation never falls through to the global DB. |

### memory

| Action | Summary |
|--------|---------|
| `mem_dedupe` | Merge same-named memory entities (keep data-bearing, archive dupes). |
| `mem_list_entities` | List all memory entities (name/type/obs count) for auditing. |
| `mem_observe` | Record user-pattern evidence into the observation store (`.ai/memory-bank/observations.jsonl`) — baking-pipeline input. |
| `mem_read` | Read node details or the graph neighbourhood. |
| `mem_remove` | Archive entities or delete observations/relations (type-safe — refuses ambiguous names). |
| `mem_replay` | Replay the offline cache (`.ai/memory-bank/pending.jsonl`) — re-apply mutations queued when the store/MCP was down; failed entries are kept for retry. `dry_run` previews. |
| `mem_search` | Hybrid BM25+dense search over memory (optionally by entity type). store=patterns + scope/context for stack-scoped user patterns; store=family_<slug> for correlated-project memory. |
| `mem_write` | Create/tag entities, add observations, or relate entities. |

### plan

| Action | Summary |
|--------|---------|
| `plan_status` | Read plan/registry status: active plan, next task, completeness, phase gate. |
| `plan_update` | Mutate plan/registry: switch active plan, mark phase complete, resolve deferred tasks. |
| `plan_doc` | Read / create / update / delete a plan's `plan.md` or `notes.md` directly (pass full content; no diff review). |
| `reg_update` | Single registry.md CRUD: `create` (server-generated UUID, Active ⏹️, Date + immutable Created At) / `update` (status active\|paused\|complete → move to correct table, refresh Date, keep Created At, optional summary) / `delete` (strict user approval via `confirmed=true`). |

### graph

| Action | Summary |
|--------|---------|
| `graph_build` | Build/update the code knowledge graph into .ai/codegraph/ (AST-only, no LLM). family=<slug> builds the MERGED family graph (per-member builds + member:: tag merge) — correct even across drives; runtime/API calls belong in memory relations. With include_html, family.html is generated + mirrored into every member's .ai/codegraph/ (graph.html stays each project's own). |
| `graph_status` | Report code-graph freshness (exists? stale? changed files). |
| `graph_query` | Search the code graph (labels / source files / types). Auto-freshens first. |
| `graph_path` | Shortest path between two graph nodes. Auto-freshens first. |
| `graph_explain` | Explain a graph node (details + direct neighbours). Auto-freshens first. |

#### Indexed scope (read this before querying)

- The graph is **AST-only** and indexes **file / function / class / component-level labels**.
  Local/computed/ref/prop variables are **NOT** nodes.
- When `graph_query` finds **no node** for a term, it falls back to a **whole-word source scan**
  and returns file-level hits with `type: "identifier"` and `mode: "identifier"` — so a query for
  a variable (e.g. `brakeBaselineDays`) never returns a dead end.
- **Node identity is unified**: all graph actions accept BOTH a node `id` (as returned by
  `graph_query`) and a label. `graph_path`/`graph_explain` resolve id → label → source-file path →
  function-name → substring, so cross-file navigation works with labels, ids, or file paths.
- `graph_path` finds a **symbol-level** path first; if none exists it falls back to a **module-level**
  path over `imports_from`/`imports` edges (`mode: "module"`), and otherwise reports a rich
  "no path" diagnostic with both source files.
- **Vite/JS path-alias imports are indexed** (`@/stores/auth`, `@pages/...`, `~/components/...`).
  graphifyy only resolves relative imports + tsconfig/jsconfig `paths`; the bridge adds a
  post-build pass that reads `resolve.alias` from `vite.config.*` / `nuxt.config.*` (object or
  array form, including `fileURLToPath(new URL(...))` replacements) and emits the missing
  `imports_from`/`imports` edges — so `.vue` SFCs and any `@/`-importing file stay connected
  in `graph_path` even when no `tsconfig.json` exists. Alias-resolved edges carry
  `alias_resolved: true`; the pass is idempotent and also self-heals previously-built graphs
  (no full rebuild needed).

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
| `wf` | List or execute a workflow (workspace-free; shared `work-flows` dir, `workflows_dir` override). |

---

## Pattern baking & delivery

The server turns recurring user-pattern evidence into reusable candidates **deterministically (no LLM)**:

1. **Observe** — `mem_observe` (agent-relayed) or `awlab-ai-assistant.exe hook --agent <host> --event <event>`
   (host lifecycle events) append signals to `.ai/memory-bank/observations.jsonl` (dedup by fingerprint).
2. **Bake** — every `action_call` runs an inline `bake_tick`: read → key → count → consistency →
   confidence. Candidates are written to `.ai/memory-bank/baked.json` only when they change.
   Confidence = `frequency(min(1,count/5)) × consistency × source_weight` (`explicit`/`corrected` 0.9,
   `behavioral` 0.6, `inferred` 0.4). A candidate needs `count ≥ 2 ∧ consistency ≥ 0.5 ∧ confidence ≥ 0.6`.
3. **Deliver (tell-once)** — `ctx_info mode="context"` / `mem_search store="patterns"` return
   `pattern_candidates` / `baked_patterns` (stack-scoped). The delivery marker records told signatures
   so a candidate is NEVER re-told until new evidence bakes.

**Three tiers, one store** — inline (per `action_call`), async (background `bake-scheduler` re-bakes
active workspaces), and subagent (`awlab-baker`, gated by new candidates) all share the same
`observations.jsonl` + `baked.json`, so candidates are identical regardless of tier.

**Hook mode** — `awlab-ai-assistant.exe hook --agent <host> --event <event>` captures observations from
host lifecycle events (user prompt, tool use, session start/stop, subagent stop) with **zero LLM cost**.
Registration is per-host; the exe derives the project per event (see `docs/en/INSTALL.md`).

## Offline cache (`pending.jsonl`)

When a store write fails or the MCP server is unreachable, intended mutations are
**queued — never dropped** — to `.ai/memory-bank/pending.jsonl` (JSONL: one JSON
object per line):

- **Server-side (automatic):** `mem_write`/`mem_remove` store down or `task_update`
  DB-sync down → the operation is queued automatically.
- **Agent-side (MCP down):** append the intended `mem_write` / `mem_remove` /
  `task_update` as one JSONL line with your own file tools, never claim success
  (rule `14-mcp-offline-cache`).
- **Replay:** `mem_replay` drains the queue — successful entries are removed,
  failed ones kept for retry; `dry_run` previews first.

## Project families

Correlated projects at different paths share a merged code graph and a dedicated
`family_<slug>` memory store. `project-families.json` (v2) declares members as
`[{path, project_id}]` — the project's own `.ai/project-id` is authoritative over
the declared id (reconciled automatically on family build), fresh members are
seeded, and `graph_build` with `family=<slug>` produces the merged graph with
`<project_id>::`-tagged nodes.
