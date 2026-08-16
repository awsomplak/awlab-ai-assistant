<!-- → authority: 00-meta.md -->
# MCP Server Per-Project Isolation

## Strategy

**Per-Project Memory Namespace** (implemented by the `awlab-ai-assistant` wrapper)

Each project gets its own isolated memory namespace driven by `AGENT_RECALL_SLUG`. The slug is derived from the workspace folder name and stored in `.ai/project-id`.

There is **ONE** MCP server: `awlab-ai-assistant` (single executable `dist/bin/awlab-ai-assistant.exe`). It exposes exactly 2 tools — `action_call` + `action_help` — that route the 23 actions (`task_*`, `plan_*`, `mem_*`, `graph_*`, `ctx_*`, `util_*`, `wf`, `reg_*`).

## Isolation Mechanism

The `awlab-ai-assistant` wrapper script reads `.ai/project-id` from the current project root and sets `AGENT_RECALL_SLUG`, which routes all memory operations to the correct project-specific namespace.

- `AGENT_RECALL_SLUG` is set by the wrapper script from `.ai/project-id`
- Sanitized: lowercase, non-alphanumeric → `_`, collapse underscores
- Each project's memories are completely isolated from every other project

## Action Mapping

All memory operations use the `awlab-ai-assistant` memory actions. See `01-memory-bank.md` for the full reference:

| Action | Covers (legacy aliases) | Purpose |
|--------|--------------------------|---------|
| `mem_write` | `mem_create_entities`, `mem_tag_entity`, `mem_relate`, `mem_store` | Create/tag entities, add observations, relate |
| `mem_search` | `mem_list_patterns` | Unified search (project_id + scope + use_dense) |
| `mem_read` | `mem_fetch_node_details`, `mem_read_graph` | Node details / graph neighbourhood |
| `mem_remove` | `mem_archive_entities`, `mem_delete_relations`, `mem_delete_observations` | Archive entities, delete observations/relations |

## Routing Rule

- Call everything through `action_call(action="...", params={...})`; use `action_help(action="...")` for usage.
- The local `mcp_server/` directory is standalone and not used by the rules.

## Implementation

The isolation is handled entirely by the `awlab-ai-assistant` wrapper script, which reads `.ai/project-id` and sets `AGENT_RECALL_SLUG`. No manual bridge code is needed.

See `08-project-id.md` for the auto-detection bootstrap protocol.