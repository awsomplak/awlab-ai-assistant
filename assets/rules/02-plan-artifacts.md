<!-- → authority: 00-meta.md -->
# Plan Artifacts Rules

## CRITICAL: Use MCP Actions, Not Manual File Edits

All plan operations **MUST use `action_call` on the `awlab-ai-assistant` server**. Do NOT manually
read/write `tasks.md`, `registry.md`, or `plan.md` files — the actions handle file I/O
atomically with proper locking.

| Operation | Action to Call | What it does |
|-----------|----------------|--------------|
| Plan status | `plan_status` | Active plans + next eligible task + completable (+ phase gate via `phase`) |
| Switch plan | `plan_update` (mode=`switch`) | Changes active plan |
| Read tasks | `task_read` | Returns tasks as structured/raw/minimal JSON |
| Update task(s) | `task_update` | Create or update tasks (multi-level paths, atomic, auto-create) |
| Validate gate | `plan_status` (params: `phase=N`) | Checks predecessor phase |
| Next task | `plan_status` | Returns `next_task` for active plan |
| Mark phase done | `plan_update` (mode=`mark_phase`, `phase_number`) | Completes all tasks in phase |
| Resolve deferred | `plan_update` (mode=`resolve`) | Re-evaluates ⏳ tasks |
| Check complete | `plan_status` | Returns `completable` |
| Write tasks file | `task_update` (params: `content=...`) | Persists task markdown atomically |
| Format tasks | `task_update` (params: `format="markdown"`, `phases`) | Returns markdown string (does NOT save) |
| Generate mermaid | `util_info` (mode=`mermaid`) | Returns flowchart text |
| Validate status | `task_update` | Internal transition validation — returns `valid_targets` on illegal transition |
| Context snapshot | `ctx_info` (mode=`snapshot`) | Active plan + patterns + project ID |

## Mandatory Task Tracking (ALWAYS Required)

After **every** completed implementation step, the agent **MUST** immediately update `tasks.md` via `action_call(action="task_update", params={...})`:

| Action | Example |
|--------|---------|
| Single task done | `action_call(action="task_update", params={"updates": [{"task_path": "1.2", "new_status": "[x]"}]})` |
| Multiple tasks done | `action_call(action="task_update", params={"updates": [{"task_path": "1.2", "new_status": "[x]"}, {"task_path": "2.1", "new_status": "[x]"}]})` |

**Rules:**
1. Update tasks **immediately** after finishing each task — never batch at the end of a session.
2. Use one `task_update` call with an `updates` array for one or more tasks.
3. Always read the current `tasks.md` first (`task_read`) to confirm the correct task path.
4. Do NOT rely on memory or todo lists — the tasks.md file is the single source of truth.
5. When tasks in `tasks.md` become outdated (e.g., scope changed), use `task_update` with `content` to rewrite them with accurate status.
6. Mark phases complete via `plan_update` (mode=`mark_phase`) only when ALL tasks in that phase are done.
7. Auto-create a missing phase/task by including `description` in an update — the path still must follow the STRICT numbering rules below.
8. **Complete-before-advance** — mark a task `[x]` immediately after it is verified done, BEFORE starting the next task; never leave a completed task unchecked while advancing.

## Plan-First & Record-Before-Execute (MANDATORY)

- **Plan-first, verify-after** — during plan creation do ONLY the required bootstrap
  (env detect, registry read, pattern load, memory populate). No verification, tests,
  fixture builds, or source exploration before the plan is presented and approved.
- **Record-before-execute** — any new work discovered outside plan/notes/tasks is
  recorded into the plan (new task/note) BEFORE executing it.
- **Verify-before-claim** — when you state that something was recorded/updated in the
  plan (tasks.md / notes.md / plan.md), ACTUALLY perform the write (via `task_update`
  or direct edit) and CONFIRM it persisted before claiming it. Never claim "recorded"
  for a change you did not write and re-read; if a prior claim was false, correct the
  artifact first, then restate.
- **Resolve-before-write** — verify imports resolve and types are clean (no
  unresolved-import/type warnings) BEFORE and AS writing code; run get_errors after
  every edit and never leave unresolved import warnings (a wrong relative-import depth
  surfaces here first).
- **Hygiene** — never emit `depends: none`; never put internal plan references (plan
  UUIDs, "(new action, plan X)") in production docs/artifacts — keep provenance in
  plan artifacts + memory only.

## Task Path & Phase Numbering (STRICT — parsing depends on it)

The server parses `tasks.md` and resolves every task by its dotted **path**. A malformed
phase number or path **breaks parsing silently** (tasks get merged into the wrong phase),
so follow these rules EXACTLY:

1. **Phase numbers are sequential POSITIVE INTEGERS only**: `## Phase 1:`, `## Phase 2:`, …
   - ❌ NEVER decimals (`## Phase 12.5:`), letters (`## Phase A:`), or gaps-implying labels.
   - A phase header is `## Phase <integer>: <goal>` — exactly one space after `Phase`, then
     the integer, then `:`. Do not add sub-phase numbering like `12.5`.
2. **Task paths are dot-separated positive integers**: `1.2` = Phase 1, Task 2;
   `1.2.3` = Phase 1, Task 2, Subtask 3.
   - ❌ NEVER non-numeric segments (`1.x`, `1.task`), decimals (`1.5.1`), or letters.
   - The `.` separator is reserved for path segments — a decimal phase would make
     `12.5.1` ambiguous, so phases must never contain `.`.
3. **Every phase number must be unique and match a `## Phase N:` header.** If you need to
   insert work between existing phases, RE-NUMBER the integer phases sequentially —
   never introduce a fractional phase.
4. **Indentation defines nesting**: subtasks are indented deeper than their parent task
   (4 spaces per level, matching the template). Use the same indentation for every task at
   the same level.
5. **Use `task_read` first** to see the exact existing paths before updating; then
   `task_update` with those exact paths. Never invent a path that `task_read` did not show.

The format descriptions below are **for reference only** — the actions handle formatting
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
Memory operations go through the `mem_*` actions via `action_call`.

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

The registry is a single markdown file with three tables — use `action_call(action="plan_status")` to read it:

| Status | Meaning |
|--------|---------|
| ⏹️ | Active plan (max 1) |
| ⏸️ | Paused plan |
| 🔄 | Retrospective in progress |
| ✅ | Completed plan |

**Archiving**: Completed plans stay forever. No archive file. `plan_status` (completable)
and `plan_update` (mark_phase) handle status transitions. Do not edit tables manually.

## Plan Structure (Reference Only)

Each plan at `.ai/artifacts/{uuid}/`:
- `plan.md` — Approach, reasoning, outcomes
- `tasks.md` — Ordered checklist by phases
- `notes.md` — Constraints, risks, decisions (OPTIONAL, keep compact)

**notes.md discipline (no bloat):** notes.md holds only significant constraints, risks, and
key decisions. Do NOT append per-phase completion summaries or implementation detail — phase
state lives in `tasks.md` markers, the registry, and `plan.md`. `plan_update` returns a
`notes_summary` for the response only; it does NOT write notes.md and neither should you. At
most update the `## Status` tracker line when a phase completes.

Use `action_call(action="task_update", params={"content": "..."})` to persist tasks.
Never write `tasks.md` directly.

## Tasks Format (Reference Only)

    # Tasks

    ## Phase 1: {phase goal}            <- integer phase number ONLY
    - [ ] Task 1: {description}         <- path 1.1
        - [ ] Task 1.1: {description}   <- path 1.1.1 (4-space indent = subtask)
    - [ ] Task 2: {description}         <- path 1.2
        → depends: 1.1                  <- depends uses dotted integer paths
    - [ ] Task 3: {description}         <- path 1.3
        ? if: condition_met

    ## Phase 2: {next goal}             <- next integer: 2 (never 1.5, never 2a)

Status markers: `[ ]` Pending · `[x]` Done · `[x✓]` Tested · `[x!]` Warnings
               `[!]` Failed · `[—]` Skipped · `[⏳]` Deferred

**Every phase number and every path segment MUST be a positive integer.**
`→ depends:` and `? if:` reference paths with the same dotted-integer notation.
Only emit `→ depends:` when a real dependency exists — NEVER write `depends: none`
(omit the line entirely).

Use `task_update` (params: `updates`) to change markers.  
Use `task_update` (params: `format="markdown"`, `phases`) to preview without saving.  
Use `task_update` (params: `content`) to save.  
Do NOT edit `tasks.md` directly.

## Constraints

- Only ONE active plan per project at a time
- The `plan-creator` skill pauses the current active plan when creating a new one
- Plans are documentation only — no implementation during creation
- Keep `registry.md` accurate — use MCP tools, not manual edits
- Each project's artifacts are completely independent