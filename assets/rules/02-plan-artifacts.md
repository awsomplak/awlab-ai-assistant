<!-- → authority: 00-meta.md -->
# Plan Artifacts Rules

## CRITICAL: Use MCP Tools, Not Manual File Edits

All plan operations **MUST use awlab-plan MCP tools**. Do NOT manually read/write
`tasks.md`, `registry.md`, or `plan.md` files — the tools handle file I/O atomically
with proper locking.

| Operation | Tool to Call | What it does |
|-----------|-------------|--------------|
| List plans | `reg_list_registry()` | Returns registry as JSON |
| Switch plan | `reg_switch_active_plan(plan_uuid)` | Changes active plan |
| Read tasks | `task_read_plan_tasks(plan_uuid, format)` | Returns tasks as structured JSON |
| Update task | `task_update_status(task_path, new_status)` | Changes one task's status |
| Batch tasks | `task_batch_update(updates)` | Atomically updates multiple tasks |
| Validate gate | `reg_validate_phase_gate(phase_num)` | Checks predecessor phase |
| Next task | `reg_get_next_eligible_task()` | Finds first non-blocked task |
| Mark phase done | `reg_mark_phase_complete(phase_number)` | Completes all tasks in phase |
| Resolve deferred | `reg_resolve_deferred_tasks()` | Re-evaluates ⏳ tasks |
| Check complete | `reg_check_plan_completable()` | Verifies all tasks done |
| Write tasks file | `task_write_plan_tasks(plan_uuid, content)` | Persists task markdown atomically |
| Format tasks | `task_format_markdown(phases)` | Returns markdown string (does NOT save) |
| Generate mermaid | `util_generate_mermaid(phases)` | Returns flowchart text |
| Validate status | `task_validate_transition(current, target)` | Checks legal transitions |
| Status check | `ctx_get_snapshot()` | Active plan + patterns + project ID (via awlab-mcp) |

## Mandatory Task Tracking (ALWAYS Required)

After **every** completed implementation step, the agent **MUST** immediately update `tasks.md` via:

| Action | Tool | Example |
|--------|------|---------|
| Single task done | `task_update_status(task_path, "[x]")` | `task_update_status("1.2", "[x]")` |
| Multiple tasks done | `task_batch_update(updates)` | `task_batch_update([{"task_path":"1.2","new_status":"[x]"}])` |

**Rules:**
1. Update tasks **immediately** after finishing each task — never batch at the end of a session.
2. Use `task_update_status` for a single task, `task_batch_update` for 2+ tasks.
3. Always read the current `tasks.md` first to confirm the correct task path.
4. Do NOT rely on memory or todo lists — the tasks.md file is the single source of truth.
5. When tasks in `tasks.md` become outdated (e.g., scope changed), use `task_write_plan_tasks` to rewrite them with accurate status.
6. Mark phases complete via `reg_mark_phase_complete()` only when ALL tasks in that phase are done.

The format descriptions below are **for reference only** — the tools handle formatting
automatically. Only consult them when you need to understand the file structure
(e.g., reading raw content for debugging).

---

## CRITICAL: Project-Scoped Operations

All artifact paths are relative to the CURRENT PROJECT ROOT. Before any operation:
1. Determine the project root from the current workspace folder
2. All `./.ai/` paths refer to `{project-root}/.ai/`
3. Never use a global or shared `.ai/` directory—each project has its own
4. If multiple workspaces are open, confirm which project the user intends

## Auto-Setup (No Confirmation)

If `./.ai/artifacts/` directory does not exist, create it silently along with:
- `./.ai/artifacts/registry.md` with header `# Plan Registry` and an empty table

**Note**: Memory bank directory (`.ai/memory-bank/`) is no longer created automatically.
Memory operations go through awlab-memory MCP tools.

## Plan Mode Bootstrap (Mandatory)

Before ANY plan-related operation (e.g., `create plan`, `start phase {N}`, `follow rules`
with an active plan), you **MUST** verify the directory structure exists:
- `./.ai/`
- `./.ai/artifacts/`
- `./.ai/artifacts/registry.md`
If any of these are missing, create them silently before proceeding.

## Uninitialized Recovery Protocol

If a command like `follow rules` or `start phase {N}` is executed but no active plan
is registered (⏹️) in `./.ai/artifacts/registry.md`:
1. **Permissive Q&A Exception**: If the user's request is purely investigatory,
   read-only, or diagnostic, answer immediately without triggering any blocks.
2. **Write & Execution Restrictor**: If the request writes code or alters files:
   - Scan chat history for a previously agreed verbal plan.
   - If found: prompt to scaffold into rules-compliant plan artifacts.
   - If not found: prompt to run `create plan` first.

## UUID Format

Plan UUIDs: **8-character lowercase alphanumeric** — `[a-z0-9]{8}`, no dashes.

## Registry Format (Reference Only)

The registry is a single markdown file with three tables — use `reg_list_registry()` to read it:

| Status | Meaning |
|--------|---------|
| ⏹️ | Active plan (max 1) |
| ⏸️ | Paused plan |
| 🔄 | Retrospective in progress |
| ✅ | Completed plan |

**Archiving**: Completed plans stay forever. No archive file. `reg_mark_phase_complete()`
and `reg_check_plan_completable()` handle status transitions. Do not edit tables manually.

## Plan Structure (Reference Only)

Each plan at `.ai/artifacts/{uuid}/`:
- `plan.md` — Approach, reasoning, outcomes
- `tasks.md` — Ordered checklist by phases
- `notes.md` — Constraints, risks, decisions (optional)

Use `task_write_plan_tasks(plan_uuid, content)` to persist tasks. Never write `tasks.md`
directly.

## Tasks Format (Reference Only)

    # Tasks

    ## Phase 1: {phase goal}
    - [ ] Task 1: {description}
        - [ ] Task 1.1: {description}
    - [ ] Task 2: {description}
        → depends: Task 1
    - [ ] Task 3: {description}
        ? if: condition_met

Status markers: `[ ]` Pending · `[x]` Done · `[x✓]` Tested · `[x!]` Warnings
               `[!]` Failed · `[—]` Skipped · `[⏳]` Deferred

Use `task_update_status()` / `task_batch_update()` to change markers.  
Use `task_format_markdown()` to preview without saving.  
Use `task_write_plan_tasks()` to save.  
Do NOT edit `tasks.md` directly.

## Constraints

- Only ONE active plan per project at a time
- The `plan-creator` skill pauses the current active plan when creating a new one
- Plans are documentation only — no implementation during creation
- Keep `registry.md` accurate — use MCP tools, not manual edits
- Each project's artifacts are completely independent