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
- **Exclusions** (additive, gitignore syntax): the graph always honors `_NOISE_DIRS`
  (`.git`, `.venv`, `node_modules`, `dist`, `build`, `vendor`, …) and dependency lock
  files; it also honors the project's `.gitignore`, plus a project-local **`.graphignore`**
  that lets you exclude files/dirs from the CODE GRAPH only (generated code, vendored
  copies, …) without ever affecting git. `.gitignore` and `.graphignore` are parsed into
  ONE additive rule set with IDENTICAL glob semantics for files AND directories
  (`dist-*/`, `**/cache/` prune whole subtrees; exact names/paths exclude a file or a
  directory). A bare `*`/`**` (ignore-all, which needs `!` re-inclusions) is skipped, so
  a Laravel-style `.gitignore` can never accidentally exclude every file. A `.graphignore`
  change triggers a rebuild.

#### Chunked builds (large-project performance)

On large projects a full graph build can spike RAM/CPU. To keep it smooth, `graph_build`
processes the corpus in **bounded chunks** (queue style):

| Param / Env | Default | Meaning |
|-------------|---------|---------|
| `chunk_size` / `GRAPH_CHUNK_SIZE` | `200` | Max files processed per build. Each run advances the manifest by exactly that many and returns `processed_files` / `remaining_files` / `chunked`. |
| `max_files` / `GRAPH_MAX_FILES` | (unset) | Cap the FIRST build's leading corpus (initial chunk). |
| `background` | `true` | Fire-and-forget trigger: return immediately and let the background worker process chunks until `remaining_files == 0`; set `false` to process one chunk synchronously (never blocked by an in-flight rebuild). |
| `force` | `false` | Bypass the in-flight guard and start a fresh build even if a stale rebuilding flag/worker is present (escape hatch for a stuck state). |

- **Flat resource usage** — every build touches ≤ `chunk_size` files, so peak RAM/CPU stays
  flat instead of one big spike; ideal for very large projects.
- **Observable progress** — `graph_build`/`graph_status` report `processed_files` and
  `remaining_files`; `remaining_files == 0` means the graph is complete.
- **No partial reads** — while a build is incomplete, `graph_query`, `graph_path`, and
  `graph_explain` return `mode: "pending"` with no nodes instead of serving partial or stale
  graph results. Retry after `graph_status` reports `fresh: true`.
- **Auto-completion** — a graph read (`graph_query`/`graph_path`/`graph_explain`) triggers
  `graph_fresh` → `ensure_fresh(background=True)`, which starts a background chunk worker, so
  reads also advance the build smoothly.
- `node_limit` (default `20000`, `GRAPHIFY_VIZ_NODE_LIMIT`) bounds the interactive `graph.html`;
  graphs over the limit render an aggregated community view instead of failing; `0` disables
  HTML entirely.

**`graph_status` progress & accuracy** — `processed_files` is cumulative
(`total_files − remaining_files`), plus `processed_total`, `processed_this_chunk` (per-run),
`remaining_files`, `chunked`, and exclusion visibility `scanned_files` / `excluded_files` /
`supported_files`. `rebuilding` reflects a live background worker (a stale persisted
`rebuilding: true` with no live worker is auto-cleared on read); `background_error` surfaces
the last worker failure and survives a server restart. `background: false` always processes
one synchronous chunk (never blocked by an in-flight rebuild — Bug 3 fix); `force: true`
bypasses the guard entirely.

**Large-graph HTML (`graph.html` / `family.html`)** — every rendered visualization embeds a
self-contained client-side layer so projects with thousands of nodes stay usable:

- **Filter bar** — filter nodes by file path (`src/components`), by a minimum degree
  (de-hairball the view), or enable **Focus 2-hop** to show only the neighborhood of the
  selected node. Edges are clipped to the visible nodes. A **Filters** header button
  collapses/expands the bar to free sidebar space.
- **Physics guard** — above ~2000 visible nodes the forceAtlas2 layout is disabled (it would
  freeze the browser); narrow the view with the filters, then hit **Stabilize**.
- **Resizable panes** — drag the splitter between **Node Info** and **Communities** to give
  either pane more room (double-click resets); the Node Info box scrolls internally when a
  node has a long neighbor list, so it never overlaps or pushes the Communities legend.
- **Community drill-down** — when a graph is over `node_limit` (aggregated community view),
  clicking a community node opens a searchable member list and **Load members into graph**
  rebuilds the view from that community's member nodes + edges; **Reset** returns to the
  overview. Lower `node_limit` (e.g. `1500`) to get the aggregated overview + drill-down for a
  large project.

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
- `graph_build` (explicit) during an in-flight background rebuild **coalesces** — it returns `rebuilding: true` instead of starting a duplicate build. Set `force: true` to bypass the guard and start fresh, or `background: false` to process one chunk synchronously regardless.

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
