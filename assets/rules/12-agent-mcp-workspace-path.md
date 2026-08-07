<!-- → authority: 00-meta.md -->
# Agent-MCP Workspace Path Rule

## Purpose

The `awlab-mcp` actions that operate on files require an explicit `workspace_path` parameter. The server performs **no automatic workspace detection**. The AI Agent must always pass the correct workspace root.

## Rules

### Rule 1: Always pass `workspace_path` for disk actions

When calling actions that operate on files (plans, registry, memory bank, scanning, graph), **always** include the `workspace_path` parameter:

```
action_call(action="task_read", params={
  "plan_uuid": "hulqlotc",
  "workspace_path": "d:\\Project\\IDE\\cline-ai-assisted-dev"
})
```

### Affected actions (require `workspace_path`)

- `task_read`, `task_update`
- `plan_status`, `plan_update`
- `mem_search`, `mem_write`, `mem_read`, `mem_remove`
- `graph_build`, `graph_status`, `graph_query`, `graph_path`, `graph_explain`
- `ctx_info` (all modes: snapshot / memory_bank / scan / suggest / context)
- `wf` (execute — when operating on disk-based workflows; `list` is workspace-free)

### Rule 2: `get_project_id` action is removed

The `get_project_id` tool has been **removed**. To get the project ID, read `.ai/project-id` via `ctx_info mode="memory_bank"`:

```
action_call(action="ctx_info", params={
  "mode": "memory_bank",
  "filename": "project-id",
  "workspace_path": "d:\\Project\\IDE\\cline-ai-assisted-dev"
})
```

### Rule 3: Actions that do NOT need `workspace_path`

The following actions operate on server internals and do not require `workspace_path`:

- `util_info` (mode=`info` version/metadata; mode=`mermaid` generation)
- `wf` (action=`list` — workspace-free listing)

### Rule 4: `DB_PATH` is the only env-based config

The only environment variable that controls server behavior is `DB_PATH` (overrides the agent-memory database directory). All other configuration (workspace paths, project roots, log directories) must be passed as parameters or omitted entirely.