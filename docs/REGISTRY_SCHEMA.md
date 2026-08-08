# REGISTRY Schema — action_call / action_help Dispatcher

> **Design doc** — single source of truth for the
> consolidated action surface. Everything the agent sees (`action_call` description,
> `action_help` output, generated SKILL.md) is derived from this one `REGISTRY` dict,
> so nothing can drift.

## 1. Goals & Principles

1. **One source of truth** — `REGISTRY` drives tool description, `action_help`, and SKILL.md.
2. **Server-owned orchestration** — the agent makes ONE call; the server guarantees the
   complete flow via `preconditions` + `pipeline`. No partial execution, ever.
3. **Coarse, complete actions** — 36 partial tools → 20 actions (incl. `graph_*`, plus the
   `mem_list_entities` + `mem_dedupe` memory-auditing actions, the `mem_replay` offline-cache
   replay, and the `reg_update` single registry.md CRUD).
4. **Reuse existing business logic as-is** — `src/mcp_server/tools/` handlers are called
   directly by name (`**params`). No logic rewrite.
5. **Backward compatible** — the 36 legacy tool names map onto the 20 actions (32 as `aliases`;
   3 were dropped, 3 became canonical action names), so current skills/rules keep working during
   migration.
6. **Loud failure + transparent trace** — every response includes `executed`/`skipped`;
   a failure names the exact step that failed.

## 2. File Layout

```
src/mcp_server/
├── registry.py              # NEW — REGISTRY dict + PRECONDITIONS registry (single source of truth)
├── modules/
│   ├── dispatcher.py        # NEW — action_call + action_help handlers (thin, driven by REGISTRY)
│   └── registration.py      # becomes 2 @mcp.tool: action_call + action_help
├── tools/                   # existing business logic — REUSED, unchanged
└── helpers/                 # existing helpers — REUSED
```

## 3. ActionSpec Schema

Each key in `REGISTRY` is an action name. The value is an `ActionSpec`:

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `group` | `str` | ✅ | Category: `task` \| `plan` \| `memory` \| `context` \| `util` \| `workflow` \| `graph` — groups `action_help()` overview |
| `summary` | `str` | ✅ | One-line description (used in tool description / overview) |
| `doc` | `str` | ✅ | Full usage doc (used by `action_help(action)`) |
| `handler` | `callable` | ✅ | Business logic: `async def handler(**params) -> dict` (sync allowed; dispatcher awaits if coroutine). Existing `tools/*` functions reused directly. |
| `params` | `dict` | ✅ | JSON-schema-like param spec (see §4) — drives validation + help |
| `returns` | `str` | ✅ | What the result dict contains (for `action_help`) |
| `example` | `str` | ✅ | Example invocation string (for `action_help`) |
| `preconditions` | `list[str]` | — | Ordered names into `PRECONDITIONS`, run before handler (idempotent) |
| `pipeline` | `list[str]` | — | Ordered sub-steps for build/update flows; each reports done/skipped |
| `mutates` | `bool` | — | True if it writes (affects confirmation display) |
| `aliases` | `list[str]` | — | Old tool names that resolve to this action (backward compat) |

**Shape:**

```python
REGISTRY: dict[str, dict] = {
    "task_read": {
        "group": "task",
        "summary": "Read a plan's tasks.md as JSON.",
        "doc": "Parse tasks.md into structured/raw/minimal JSON. Each task includes its "
               "resolvable dotted `path` (phase.task.subtask) for use with task_update.",
        "handler": read_plan_tasks,           # tools/plan_tools/tasks.py — reused as-is
        "params": {
            "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
            "plan_uuid":      {"type": "string", "required": True, "pattern": r"^[a-z0-9]{8}$", "desc": "8-char lowercase UUID"},
            "format":         {"type": "string", "enum": ["structured", "raw", "minimal"], "default": "structured", "desc": "Output shape"},
        },
        "returns": "{success, phases:[{phase_number, name, tasks:[{path, description, status, indent, subtasks}]}]}",
        "example": 'action_call(action="task_read", plan_uuid="mcptool1", format="structured")',
        "preconditions": ["workspace_valid", "plan_uuid_valid", "tasks_file_exists"],
        "aliases": ["task_read_plan_tasks"],
    },
    # ... (full worked examples in §8)
}

PRECONDITIONS: dict[str, callable] = {
    "workspace_valid":      _pre_workspace_valid,      # (workspace_path, params) -> (ok, note, state)
    "plan_uuid_valid":      _pre_plan_uuid_valid,
    "tasks_file_exists":    _pre_tasks_file_exists,
    "graph_dir_ready":      _pre_graph_dir_ready,      # ensure .ai/codegraph/ + README
    "graph_fresh":          _pre_graph_fresh,          # ensure graph exists / stale → incremental update
}
```

## 4. Param Schema (JSON-schema subset)

`params[name]` supports the subset needed for validation + help generation:

| Key | Type | Purpose |
|-----|------|---------|
| `type` | `str` | `string` \| `integer` \| `boolean` \| `array` \| `object` |
| `required` | `bool` | If absent, param is optional |
| `default` | `any` | Default value (fills missing optional params) |
| `enum` | `list` | Allowed values |
| `pattern` | `str` | Regex (for strings) |
| `desc` | `str` | Human description (for `action_help`) |
| `items` | `dict` | Element schema (for arrays) |

The dispatcher validates params **before** running preconditions and fails loudly listing
missing/invalid params — no downstream partial work.

## 5. PRECONDITIONS Contract

Each precondition: `async def (workspace_path, params, state) -> tuple[bool, str, dict]`
returning `(ok, note, state_update)`. It is **idempotent**: it checks whether its job is
already satisfied and skips silently if so.

```python
async def _pre_graph_fresh(workspace_path, params, state):
    """Ensure the graph exists and is up to date (incremental update if stale)."""
    if _graph_fresh(workspace_path):
        return True, "graph fresh", {}          # skipped — already satisfied
    updated = await _incremental_update(workspace_path)   # only runs when stale
    return True, f"auto-updated {updated} files", {"graph_updated": updated}
```

A precondition that returns `ok=False` aborts the whole action with a loud error naming it.

## 6. Dispatcher Flow

### `action_call(action, params)`

```
1. RESOLVE   action = REGISTRY[action]  |  alias lookup  |  fuzzy match ("did you mean")
             unknown → {success:false, error, valid_actions:[...], did_you_mean:[...]}
2. VALIDATE  params against spec.params (required / type / enum / pattern / defaults)
             invalid → {success:false, error, invalid:[{param, reason}]}
3. PRECOND   for pc in spec.preconditions (in order):
               ok, note, st = PRECONDITIONS[pc](workspace_path, params, state)
               ok?  → record executed=[pc] or skipped=[pc] (idempotent)
               !ok  → ABORT: {success:false, error, failed_precondition: pc, note}
4. PIPELINE  for step in spec.pipeline (in order):
               run → record executed/skipped; any failure → ABORT (no partial commit)
5. HANDLER   result = await spec.handler(**validated_params)   # sync allowed
6. RETURN    {success, result, executed:[...], skipped:[...]}
```

### `action_help(action=None)`

```
action_help()            → grouped overview: group → actions → summary (from REGISTRY)
action_help(action)      → full spec: params+defaults+enum, example, preconditions,
                          pipeline, returns, aliases, errors
action_help("fuzzy...")  → did-you-mean suggestions + nearest actions
```

## 7. Generation (no drift)

| Output | Generator | Source |
|--------|-----------|--------|
| `action_call` tool description | `build_tool_description()` | `REGISTRY` summaries |
| `action_help` output | `build_help(action)` | full `ActionSpec` |
| SKILL.md | `build_skill_md()` | `REGISTRY` (all actions, grouped) |

All three read the **same dict** — editing one entry updates every surface.

## 8. Worked Examples

### `task_update` (merges 5 task tools — create-or-update, multi-level)

```python
"task_update": {
    "group": "task",
    "summary": "Create or update tasks.md / tasks (multi-level paths, atomic).",
    "doc": "If tasks.md is missing, create it (from `content` or a skeleton). If present, "
           "apply targeted `updates`. Task paths are multi-level dotted (phase.task.subtask). "
           "A missing task/phase is auto-created when `description` is provided. Transition "
           "validation is internal; on an illegal transition the response returns valid_targets.",
    "handler": update_tasks,                    # NEW wrapper over batch_update_tasks + write_plan_tasks
    "params": {
        "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
        "plan_uuid":      {"type": "string", "required": True, "pattern": r"^[a-z0-9]{8}$", "desc": "8-char lowercase UUID"},
        "content":        {"type": "string", "desc": "Full markdown to write (upsert whole file)"},
        "updates":        {"type": "array", "items": {"type": "object"}, "desc": "[{task_path, new_status, description?}]"},
        "auto_create":    {"type": "boolean", "default": True, "desc": "Create file/phase/task if missing"},
    },
    "returns": "{success, executed, skipped, updated:[{task_path, old_status, new_status}], created:[paths], valid_targets?}",
    "example": 'action_call(action="task_update", plan_uuid="mcptool1", updates=[{"task_path":"1.2","new_status":"[x]"}])',
    "preconditions": ["workspace_valid", "plan_uuid_valid"],
    "mutates": True,
    "aliases": ["task_update_status", "task_batch_update", "task_write_plan_tasks",
                "task_validate_transition", "task_format_markdown"],
}
```

### `mem_search` (absorbs `mem_list_patterns`)

```python
"mem_search": {
    "group": "memory",
    "summary": "Hybrid BM25+dense search over memory (optional scope/project_id).",
    "handler": search_memory,                    # tools/memory_tools.py — reused as-is
    "params": {
        "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
        "query": {"type": "string", "required": True, "desc": "Search query"},
        "project_id": {"type": "string", "desc": "Optional project scope"},
        "scope": {"type": "string", "enum": ["project", "user", "conversation"], "default": "project"},
        "limit": {"type": "integer", "default": 10, "desc": "Max results"},
        "use_dense": {"type": "boolean", "default": False, "desc": "Enable BM25+dense re-rank"},
    },
    "example": 'action_call(action="mem_search", query="registry schema", scope="project")',
    "aliases": ["mem_list_patterns"],             # list_patterns == search "type: pattern"
}
### `graph_build` (demonstrates pipeline + preconditions)

```python
"graph_build": {
    "group": "graph",
    "summary": "Build/update the project knowledge graph into .ai/codegraph/ (atomic).",
    "handler": _graph_build,                     # helpers/graphify_bridge.py
    "params": {
        "workspace_path": {"type": "string", "required": True, "desc": "Absolute path to project root"},
    },
    "preconditions": ["workspace_valid", "graph_dir_ready"],
    "pipeline": ["scan_source", "extract", "build_graph", "cluster", "export_html", "write_state"],
    "example": 'action_call(action="graph_build", workspace_path="D:/Project/Foo")',
    "mutates": True,
}
```

**Freshness contract (all `graph_*` reads).** `graph_query`, `graph_path` and
`graph_explain` each return `graph_fresh`, `graph_exists`, `graph_rebuilding` and
`graph_built_at` so the agent can tell whether the data it read is current and
whether a rebuild is in flight. A stale graph with many changed files (≥ 20), or
on a first build, is rebuilt in a **background thread** so the read never blocks:
the read reports `graph_rebuilding: true` and may serve the previous graph until
the rebuild finishes (graph.json is written atomically, so a concurrent read
never sees a partial file). Small incremental rebuilds stay synchronous so a read
right after an edit returns accurate results. Explicit `graph_build` coalesces
with an in-flight background rebuild (returns `rebuilding: true`, no duplicate
build). Builds are serialized per project (a per-workspace lock); different
projects build concurrently.

## 9. Backward-Compat Alias Map (36 → 20)

| New action | Absorbed old tools |
|-----------|--------------------|
| `task_read` | `task_read_plan_tasks` |
| `task_update` | `task_update_status`, `task_batch_update`, `task_write_plan_tasks`, `task_validate_transition`, `task_format_markdown` |
| `plan_status` | `reg_list_registry`, `reg_get_next_eligible_task`, `reg_validate_phase_gate`, `reg_check_plan_completable` |
| `plan_update` | `reg_switch_active_plan`, `reg_mark_phase_complete`, `reg_resolve_deferred_tasks` |
| `ctx_info` | `ctx_get_snapshot`, `ctx_read_memory_bank`, `ctx_scan_project`, `ctx_suggest_files` |
| `util_info` | `util_get_version`, `util_get_project_meta` |
| `mem_search` | `mem_search`, `mem_list_patterns` |
| `mem_write` | `mem_create_entities`, `mem_tag_entity`, `mem_relate`, `mem_store` |
| `mem_read` | `mem_fetch_node_details`, `mem_read_graph` |
| `mem_remove` | `mem_archive_entities`, `mem_delete_observations`, `mem_delete_relations` |
| `mem_list_entities` | — |
| `mem_dedupe` | — |
| `wf` | `wf_execute`, `wf_list` |

**Kept as `ctx_info` sub-modes** (not dropped — code still routes them): `ctx_scan_project`
→ `ctx_info mode="scan"` (used by plan-creator), `ctx_suggest_files` → `ctx_info mode="suggest"`.

**Dropped entirely** (no REGISTRY consumer): `ctx_store`, `ctx_get_fragment`
(folded into `mem_*`), `reg_generate_retrospective` (fold into pattern extraction / `mem_write`).

## 10. Open Questions — RESOLVED

1. **Sync/async handler wrapper** — ✅ RESOLVED: dispatcher calls sync handlers directly and
   `await`s coroutines via `_maybe_await` (`src/mcp_server/modules/dispatcher.py`). No wrapping
   needed per handler.
2. **`ctx_scan_project` / `ctx_suggest_files`** — ✅ RESOLVED: **kept** as `ctx_info` sub-modes
   (`mode="scan"`, `mode="suggest"`). Plan-creator still uses the scan path; not superseded.
3. **`task_update` file bootstrap** — ✅ RESOLVED: when tasks.md is missing and only `updates`
   given, `task_update` auto-creates a `# Tasks` skeleton + the referenced phase/task chain
   (`create_task_in_md`).
4. **Alias dispatch** — ✅ RESOLVED: aliases resolve to canonical actions via `_ALIAS_INDEX`
   (`resolve_action`); the response reports the canonical action name. Alias-usage logging is
   **not** implemented — old names still work but are not counted (acceptable: docs/rules are
   already migrated off them).
