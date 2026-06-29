<!-- → authority: 00-meta.md -->
# Command Reference

## Session Commands

| User Input | Action |
|------------|--------|
| `follow rules` | 1. Read `.ai/project-id`. 2. Run `mem_search(query="current project context")`. 3. Call `reg_list_registry()` for active plan (⏹️). 4. Fallback: if no active plan, offer to scaffold via plan-creator skill. 5. Call `task_read_plan_tasks(plan_uuid, format="minimal")` for task summary. |
| `create plan` | Activate the `plan-creator` skill to generate a new plan and populate memory via awlab-memory tools. |
| `/switch-plan {uuid}` | Use `reg_list_registry()` to find the plan, then `reg_switch_active_plan(uuid="{uuid}")`. |
| `/plan-status` | Use `ctx_get_snapshot()`. |
| `/memory [list\|approve\|reject\|prune]` | Manage memory via awlab-memory tools: `mem_search`, `mem_archive_entities`, `mem_delete_observations`. |
| `memory maintenance` | Activate the memory-maintenance skill. |
| `start phase {N}` | Gate via `reg_validate_phase_gate(plan_uuid="<active>", phase_num=N)`. If pass, implement tasks using `task_update_status`, `task_batch_update`, `reg_mark_phase_complete`, `reg_resolve_deferred_tasks`. |
| `summarize session` | Use `mem_tag_entity(observations=[{"entityName": "Session Summary", "contents": ["Session summary: ..."]}])`. |
| `project-id` | Display the current project ID from `.ai/project-id`. |
| `patterns list` | Use `mem_list_patterns()`. |
| `patterns delete <name>` | Use `mem_archive_entities(names=["pattern_{name}"])`. |
| `patterns deprecate` | Remove stale patterns via `mem_search` + `mem_archive_entities`. |
| `patterns export` | Export all patterns to `./.ai/patterns_export.json`. |

## Project Scanning

- **Allowed**: Scan project source directories for code analysis and understanding
- **Required**: During plan creation, use `ctx_scan_project()`.
- **Forbidden**: Scan `./.ai/artifacts/` directory (use registry.md via `reg_list_registry`)
- **Forbidden**: Scan `./.ai/memory-bank/` directory (except `environment.md` via `ctx_read_memory_bank`)

## Task Tracking

→ see: `02-plan-artifacts.md` — all task execution delegated to awlab-plan MCP tools (`task_update_status`, `task_batch_update`, `reg_validate_phase_gate`, `reg_get_next_eligible_task`, `reg_mark_phase_complete`, `reg_resolve_deferred_tasks`, etc.)

## Multi-Model Routing

→ see: `07-model-router.md` for task classification, escalation protocols, and model-aware behavior.
