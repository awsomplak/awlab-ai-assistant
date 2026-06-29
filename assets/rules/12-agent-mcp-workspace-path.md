<!-- → authority: 00-meta.md -->
# Agent-MCP Workspace Path Rule

## Purpose

The MCP server tools that operate on files require an explicit `workspace_path` parameter. The servers perform **no automatic workspace detection**. The AI Agent must always pass the correct workspace root.

## Rules

### Rule 1: Always pass `workspace_path` for disk tools

When calling MCP tools that operate on files (plans, registry, memory bank, scanning), **always** include the `workspace_path` parameter:

```xml
<use_mcp_tool>
<server_name>awlab-plan</server_name>
<tool_name>task_read_plan_tasks</tool_name>
<arguments>
{
  "plan_uuid": "hulqlotc",
  "workspace_path": "d:\\Project\\IDE\\cline-ai-assisted-dev"
}
</arguments>
</use_mcp_tool>
```

### Affected tools (require `workspace_path`)

- `task_read_plan_tasks`
- `task_write_plan_tasks`
- `task_update_status`
- `task_batch_update`
- `task_format_markdown` (optional, only when `plan_uuid` provided)
- `reg_validate_phase_gate`
- `reg_get_next_eligible_task`
- `reg_mark_phase_complete`
- `reg_resolve_deferred_tasks`
- `reg_check_plan_completable`
- `reg_list_registry`
- `reg_switch_active_plan`
- `reg_generate_retrospective`
- `ctx_read_memory_bank`
- `ctx_get_snapshot`
- `ctx_suggest_files`
- `ctx_scan_project` (unified — use force_refresh to bypass cache)
- `wf_execute` (when operating on disk-based workflows)

### Rule 2: `get_project_id` MCP tool is removed

The `get_project_id` MCP tool has been **removed**. To get the project ID, read `.ai/project-id` directly using `ctx_read_memory_bank`:

```xml
<use_mcp_tool>
<server_name>awlab-mcp</server_name>
<tool_name>ctx_read_memory_bank</tool_name>
<arguments>
{
  "filename": "project-id",
  "workspace_path": "d:\\Project\\IDE\\cline-ai-assisted-dev"
}
</arguments>
</use_mcp_tool>
```

### Rule 3: Tools that do NOT need `workspace_path`

The following tools operate on the knowledge graph or server internals and do not require `workspace_path`:

- `mem_search`, `mem_create_entities`, `mem_tag_entity`, `mem_relate`, `mem_fetch_node_details`, `mem_read_graph`, `mem_archive_entities`, `mem_delete_observations`, `mem_delete_relations`
- `mem_search`, `mem_store`, `mem_list_patterns`, `ctx_store`, `ctx_get_fragment`
- `task_validate_transition`
- `util_get_project_meta`, `util_get_version`
- `util_generate_mermaid`
- `wf_list` (workspace-free listing)

### Rule 4: `DB_PATH` is the only env-based config

The only environment variable that controls server behavior is `DB_PATH` (overrides the agent-memory database directory). All other configuration (workspace paths, project roots, log directories) must be passed as parameters or omitted entirely.