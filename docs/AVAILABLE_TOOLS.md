# Available MCP Tools

36 tools across 3 MCP servers, split to work around Copilot's per-server tool visibility limit (~15 tools/server).

---

## 🔧 awlab-mcp (6 tools — Utility & Context)

| Tool | Description |
|------|-------------|
| `util_get_version` | Return MCP server build version and build tag |
| `util_get_project_meta` | Return OS, shell, server version, workspace metadata |
| `ctx_get_snapshot` | Get active plan, patterns, and project ID |
| `ctx_read_memory_bank` | Read allowed files from `.ai/memory-bank/` |
| `ctx_scan_project` | Framework-aware project scan (entry points, relationships) |
| `ctx_suggest_files` | Suggest relevant files for a task description |

**Binary**: `awlab-mcp.exe` · **Entry point**: `src/mcp_server/__main__.py`

---

## 📋 awlab-plan (17 tools — Registry, Tasks & Workflows)

### Registry Lifecycle

| Tool | Description |
|------|-------------|
| `reg_list_registry` | Return Active/Paused/Completed plans as JSON |
| `reg_switch_active_plan` | Change the active plan in the registry |
| `reg_validate_phase_gate` | Check if predecessor phase is complete |
| `reg_get_next_eligible_task` | Find next non-blocked, non-terminal task |
| `reg_mark_phase_complete` | Mark all tasks in a phase as completed |
| `reg_resolve_deferred_tasks` | Re-evaluate ⏳ deferred tasks |
| `reg_check_plan_completable` | Verify all tasks are in terminal states |
| `reg_generate_retrospective` | Extract patterns from a completed plan |

### Task Management

| Tool | Description |
|------|-------------|
| `task_read_plan_tasks` | Parse `tasks.md` into structured/raw/minimal JSON |
| `task_write_plan_tasks` | Write markdown content to `tasks.md` |
| `task_update_status` | Update a single task's status marker |
| `task_batch_update` | Atomically update multiple tasks with rollback |
| `task_validate_transition` | Check if a status marker transition is legal |
| `task_format_markdown` | Format task list as markdown |

### Workflows & Utilities

| Tool | Description |
|------|-------------|
| `wf_execute` | Execute a named workflow |
| `wf_list` | List available workflow files |
| `util_generate_mermaid` | Generate Mermaid flowchart from phases |

**Binary**: `awlab-plan.exe` · **Entry point**: `src/mcp_server/__main_plan__.py`

---

## 🧠 awlab-memory (13 tools — Memory & Context Store)

### Knowledge Graph

| Tool | Description |
|------|-------------|
| `mem_search` | Hybrid BM25+dense search (supports scope + project_id) |
| `mem_store` | Store an observation (auto-creates entity if needed) |
| `mem_create_entities` | Create new entities (people, concepts, patterns) |
| `mem_tag_entity` | Attach context labels to an existing entity |
| `mem_relate` | Declare a semantic association between entities |
| `mem_fetch_node_details` | Query specific attributes of graph nodes |
| `mem_read_graph` | Read the knowledge graph neighbourhood |
| `mem_archive_entities` | Move entities to historical archive state |
| `mem_delete_observations` | Remove specific observations from entities |
| `mem_delete_relations` | Remove relations between entities |
| `mem_list_patterns` | List all stored patterns |

### Context Store (TTL-based)

| Tool | Description |
|------|-------------|
| `ctx_store` | Store context fragments with TTL-based expiry |
| `ctx_get_fragment` | Retrieve topic-specific context without file reads |

**Binary**: `awlab-memory.exe` · **Entry point**: `src/mcp_server/__main_memory__.py`

---

## Server Architecture

```
3 Standalone Executables
├── awlab-mcp.exe         →  awlab-mcp      →  6 tools  (utility & context)
├── awlab-plan.exe        →  awlab-plan     →  17 tools (plan, registry, tasks, workflows)
└── awlab-memory.exe      →  awlab-memory   →  13 tools (memory, knowledge graph, context)
```

All servers use the same shared codebase at `src/mcp_server/`:
- **`helpers/`**: agent_recall, file_utils, registry_utils, embeddings, hybrid_search, validation, response, logger
- **`tools/`**: plan_tools, memory_tools, utils_tools, file_tools, context_tools
- **`modules/`**: lifecycle, registration, registration_plan, registration_memory

## Workspace Resolution

Parameter-driven — no auto-detection. The AI agent passes `workspace_path` explicitly to all file-operating tools. DB path resolves via 4-level fallback:

1. `DB_PATH` environment variable
2. `.ai/project-id` file in the project root
3. `~/.awlab-id/agent-memory/memory/` (production config)
4. Default workspace-relative path

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `DB_PATH` | Override agent-recall database directory | *(workspace-relative)* |
| `AGENT_RECALL_CMD` | Override agent-recall executable | *(auto-detected)* |
| `LOG_ENABLED` | Enable/disable logging | `false` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `AWLAB_ENV` | Force `production` or `development` mode | *(auto-detected)* |
