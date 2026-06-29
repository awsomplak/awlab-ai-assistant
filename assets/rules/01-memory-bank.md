<!-- → authority: 00-meta.md -->
# Memory Bank (Powered by awlab-memory)

**CRITICAL**: Your Memory Bank is now managed by the `awlab-memory` MCP server.  
All memory operations use the knowledge graph tools listed below.  
The old markdown files (`.ai/memory-bank/*.md`) are DEPRECATED, except `environment.md`.

## Auto‑Setup

If `./.ai/memory-bank/memory/` does not exist, create it silently.

## Core MCP Tools (awlab-memory)

| Tool | Purpose |
|------|---------|
| `mem_create_entities` | Create new nodes (people, decisions, patterns, concepts) |
| `mem_tag_entity` | Tag an entity with additional context labels |
| `mem_search` | Search memory (hybrid, supports project_id + scope + use_dense params) |
| `mem_relate` | Declare a semantic association between two entities |
| `mem_fetch_node_details` | Query specific attributes of localized graph nodes |
| `mem_read_graph` | Explore the neighbourhood of an entity |
| `mem_archive_entities` | Move entities to a historical archive state |
| `mem_delete_relations` | Remove relations between entities |
| `mem_delete_observations` | Remove specific observations from entities |
| `mem_store` | Store an observation (creates entity if needed) |
| `mem_list_patterns` | List all stored patterns |
| `ctx_store` | Store context fragments with TTL-based expiry |
| `ctx_get_fragment` | Retrieve topic-specific context without file reads |

## Mandatory Workflow

### Project Isolation – Automatic
- The wrapper script reads `.ai/project-id` and sets `AGENT_RECALL_SLUG`.
- All memory operations are automatically scoped to that slug – **no manual `project` parameter needed**.

### Before any task (including `follow rules`)
- Run `mem_search` with the task description to retrieve relevant context.
- Do NOT read `.ai/memory-bank/*.md` except `environment.md`.

### During a task (call tools directly — no XML wrapping needed)
- **New concept** → `mem_create_entities(entities=[{"name": "Concept", "entityType": "concept", "observations": ["description"]}])`
- **Tag entity** → `mem_tag_entity(observations=[{"entityName": "Entity", "contents": ["fact"]}])`
- **Link entities** → `mem_create_entities` + `mem_tag_entity` + `mem_relate`
- **Task completion** → `mem_tag_entity(observations=[{"entityName": "Progress", "contents": ["Completed: Phase N: description"]}])`
- **Store pattern** → `mem_store(entity_name="pattern_name", observation="value: ...", pattern_type="preference")`
- **List patterns** → `mem_list_patterns(workspace_path="...")`
  <server_name>awlab-memory</server_name>
  <tool_name>mem_list_patterns</tool_name>
  <arguments>
  {}
  </arguments>
  </use_mcp_tool>
  ```
- **Context snapshot** →
  ```xml
  <use_mcp_tool>
  <server_name>awlab-mcp</server_name>
  <tool_name>ctx_get_snapshot</tool_name>
  <arguments>
  {}
  </arguments>
  </use_mcp_tool>
  ```

### At phase/plan completion
- Run final `mem_search` to capture learnings, then consolidate with `mem_tag_entity`.

### Exception: `environment.md`
- The file `./.ai/memory-bank/environment.md` is **KEPT** only for shell command syntax detection (see `05-environment.md`). All other markdown (`.md`) files in `.ai/memory-bank/` are ignored.