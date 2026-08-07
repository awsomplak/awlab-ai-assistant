<!-- → authority: 00-meta.md -->
# Command Reference

## Session Commands

| User Input | Action |
|------------|--------|
| `follow rules` | 1. Read `.ai/project-id`. 2. Run `action_call(action="mem_search", params={"query": "current project context"})`. 3. Call `action_call(action="plan_status")` for active plan (⏹️). 4. Fallback: if no active plan, offer to scaffold via plan-creator skill. 5. Call `action_call(action="task_read", params={"plan_uuid": "<uuid>", "format": "minimal"})` for task summary. |
| `create plan` | Activate the `plan-creator` skill to generate a new plan and populate memory via `mem_write`. |
| `/switch-plan {uuid}` | Use `plan_status` to find the plan, then `action_call(action="plan_update", params={"mode": "switch", "plan_uuid": "{uuid}"})`. |
| `/plan-status` | Use `action_call(action="ctx_info")` (snapshot) or `action_call(action="plan_status")`. |
| `/memory [list\|approve\|reject\|prune]` | Manage memory via `mem_search`, `mem_remove`. |
| `memory maintenance` | Activate the memory-maintenance skill. |
| `start phase {N}` | Gate via `action_call(action="plan_status", params={"phase": N})`. If pass, implement tasks using `task_update` (updates), `plan_update` (mark_phase / resolve). |
| `summarize session` | Use `action_call(action="mem_write", params={"observations": [{"entityName": "Session Summary", "contents": ["Session summary: ..."]}]})`. |
| `project-id` | Display the current project ID from `.ai/project-id`. |
| `patterns list` | Use `action_call(action="mem_search", params={"entity_type": "pattern"})`. |
| `patterns delete <name>` | Use `action_call(action="mem_remove", params={"names": ["pattern_{name}"]})`. |
| `patterns deprecate` | Remove stale patterns via `mem_search` + `mem_remove`. |
| `patterns export` | Export all patterns to `./.ai/patterns_export.json`. |
| `orchestrate` | `action_call(action="ctx_info", params={"mode": "context", "query": "<topic>"})` → returns plan + next task + relevant code + relevant memory and atomically regenerates `.ai/memory-bank/context.md`. |

## Project Scanning

- **Allowed**: Scan project source directories for code analysis and understanding
- **Required**: During plan creation, use `action_call(action="ctx_info", params={"mode": "scan"})`.
- **Forbidden**: Scan `./.ai/artifacts/` directory (use `plan_status` for registry)
- **Forbidden**: Scan `./.ai/memory-bank/` directory (except `environment.md` via `ctx_info mode="memory_bank"`)

## Task Tracking

→ see: `02-plan-artifacts.md` — all task execution delegated to `task_update` / `plan_status` / `plan_update` via `action_call`.

## Multi-Model Routing

→ see: `07-model-router.md` for task classification, escalation protocols, and model-aware behavior.
