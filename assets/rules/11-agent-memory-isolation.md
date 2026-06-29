<!-- → authority: 00-meta.md -->
# MCP Server Per-Project Isolation

## Strategy

**Per-Project Memory Namespace** (implemented by `awlab-memory` wrapper)

Each project gets its own isolated memory namespace driven by `AGENT_RECALL_SLUG`. The slug is derived from the workspace folder name and stored in `.ai/project-id`.

Tools are split across three MCP servers:
- `awlab-mcp` — Utility & context tools
- `awlab-plan` — Registry, task, and workflow tools
- `awlab-memory` — Memory & context store tools

## Isolation Mechanism

The `awlab-memory` MCP server's wrapper script reads `.ai/project-id` from the current project root and sets `AGENT_RECALL_SLUG`, which routes all memory operations to the correct project-specific namespace.

- `AGENT_RECALL_SLUG` is set by the wrapper script from `.ai/project-id`
- Sanitized: lowercase, non-alphanumeric → `_`, collapse underscores
- Each project's memories are completely isolated from every other project

## Tool Mapping

All memory operations use `awlab-memory` or `awlab-mcp` tools. See `01-memory-bank.md` for the full tool reference:

| Server | Tool | Purpose |
|-------------------|---------|
| `mem_create_entities` | Create new entities (people, projects, tools, concepts) |
| `mem_tag_entity` | Tag an entity with additional context labels |
| `mem_search` | Unified search (supports project_id + scope + use_dense params) |
| `mem_relate` | Declare a semantic association between two entities |
| `mem_fetch_node_details` | Query specific attributes of nodes |
| `mem_read_graph` | Explore the neighborhood of an entity |
| `mem_archive_entities` | Move entities to archive state |
| `mem_delete_relations` | Remove relations |
| `mem_delete_observations` | Remove specific observations |
| `mem_store` | Store an observation (creates entity if needed) |
| `mem_list_patterns` | List all stored patterns |
| `ctx_store` | Store context fragments with TTL-based expiry |
| `ctx_get_fragment` | Retrieve topic-specific context without file reads |
| `ctx_get_snapshot` | Get active plan, patterns, and project context in one call |

## Routing Rule

- Server name in `use_mcp_tool` calls: `awlab-memory` (for memory tools) or `awlab-plan` (for plan tools) or `awlab-mcp` (for utility/context tools)
- The local `mcp_server/` directory is standalone and not used by the rules

## Implementation

The isolation is handled entirely by the `awlab-memory` MCP server's wrapper script, which reads `.ai/project-id` and sets `AGENT_RECALL_SLUG`. No manual bridge code is needed.

See `08-project-id.md` for the auto-detection bootstrap protocol.