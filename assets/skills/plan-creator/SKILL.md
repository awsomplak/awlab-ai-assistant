---
name: plan-creator
description: >
  Creates a new structured implementation plan. ONLY activate when the user
  explicitly requests a new plan with phrases like "create a new plan",
  "generate plan for", "make a detailed plan to...", "break down tasks for...",
  "create plan fo", "plan fo", "make plan fo", or uses the command "create plan", or when confirming the Uninitialized Recovery Protocol (e.g. "yes", "yes, scaffold", "go ahead", "scaffold").
  Do NOT activate for questions about existing plans, status inquiries, or memory updates.
  Operates on the active project workspace only.
---

# plan-creator

This skill handles the creation of structured implementation plans and automatic memory population via awlab-memory. It generates plan documentation, stores project analysis results using awlab-memory's tools, and maintains the plan registry. **Plans are documentation only - no code execution or implementation occurs during plan creation.**

## Usage

Activate this skill when the user explicitly requests a **new** plan using the triggers in the frontmatter. This skill always operates on the **current active project workspace**. If multiple projects are open, confirm which one the user intends.

## Steps

1. **Determine Project Root** — Identify current workspace root (per `02-plan-artifacts.md` Project-Scoped Operations). If multiple workspaces, confirm with user.

2. **Detect Environment FIRST**
    - Run environment detection per **`05-environment.md`** (the single source of truth)
    - Ensures all subsequent commands use the correct shell syntax (PowerShell vs Bash)

3. **Ensure Structure Exists** — Silent create `./.ai/` and `./.ai/artifacts/` and `registry.md` if missing. Do NOT create `./.ai/memory-bank/`.

4. **Read Project ID** — Read `.ai/project-id` (if missing, run `08-project-id.md` bootstrap first). Store as `$PROJECT_ID` (for informational use only – awlab-memory uses `AGENT_MEMORY_SLUG` automatically).

5. **Load user patterns**  
    - Run `search_nodes(query="type: pattern")`.  
    - For each result, parse observations to extract `type`, `value`, `confidence`, `timestamp`.  
    - Sort by `timestamp` descending (most recent first).  
    - Exclude patterns with `confidence < 0.3` and older than 30 days (based on `timestamp`).  
    - Store the top 5–10 patterns in a variable `$USER_PATTERNS`.

6. **Scan Project & Populate Memory via awlab-memory**
    - Run the **Fingerprint Protocol** defined in **`06-project-scanner.md`**.
    - Use scan results to call awlab-memory tools:
        - **Project architecture**:  
          `create_entities(entities=[{"name": "Project Architecture", "entityType": "concept", "observations": ["Project type: {language} / {framework}. Architecture: {pattern}. Key directories: {list}. Entry point: {file}."}])`
        - **Dependencies**:  
          `create_entities(entities=[{"name": "Dependencies", "entityType": "concept", "observations": ["Top dependencies: {list}"]}])`
        - **Testing framework**:  
          `create_entities(entities=[{"name": "Testing", "entityType": "concept", "observations": ["Framework: {name}. Location: {path}. Run command: {command}"}])`
        - **Key relationships** (max 15):  
          `create_relations(relations=[{"from": "FileA", "to": "FileB", "relationType": "depends_on"}])`
        - **Quick index** (max 20):  
          `create_entities(entities=[{"name": "{concept}", "entityType": "index", "observations": ["{file_path}: {key_info}"]}])`
    - Also store initial brief, context, progress as observations on dedicated entities.

7. **Read Registry**
    - Parse `./.ai/artifacts/registry.md` to find existing plans and UUIDs
    - Do **not** scan the `./.ai/artifacts/` directory (use registry.md only — see `03-token-strategies.md`)
    - If registry exists but table structure is malformed (missing headers or separator row), rebuild the table header and warn: "⚠️ Registry table was malformed. Headers restored. Verify plan entries."

8. **Generate Plan Identity**
    - Generate an 8-character randomized lowercase alphanumeric UUID per the format defined in `02-plan-artifacts.md` (UUID Format section).
    - Ask user for a concise one-line summary of the plan.
    - **CRITICAL: STOP HERE AND WAIT FOR THE USER's RESPONSE.** Do NOT proceed to Step 8 until the user has provided the summary.
    - **EXCEPTION**: If the user has already provided a plan summary, a clear feature goal, or specific details in their triggering prompt, bypass the stop-and-wait check entirely.

9. **Create Plan Files**
    - After the user provides the summary, create directory `./.ai/artifacts/{uuid}/`
    - **Template matching:** Check if the user's request matches a template in the local `Cline/Skills/plan-creator/templates/`:
      - `feature-crud.md` (CRUD), `auth-flow.md` (authentication), `migration.md` (migration), `refactor.md` (refactor), `bugfix.md` (bug), `integration.md` (integration).
    - If a template matches: read the skeleton, then **customize** phases and tasks based on project context from stored memories (no search needed – the architecture and patterns are already stored as entities).
    - If no template matches: generate plan from scratch.
    - Write `plan.md` with sections:
        - **Overview**
        - **User Preferences (Learned)**
          Include each pattern from `$USER_PATTERNS`, formatted as `- {type}: {value}`.
        - **Approach**
        - **Expected Outcomes**
    - Write `tasks.md` with phases and ordered checklist per `02-plan-artifacts.md` (Tasks Format and Extended Task Format). Use `→ depends:` and `? if:` markers where appropriate.
    - Write `notes.md` only if technical constraints, risks, or key decisions exist

10. **Update Registry**
    - Change all existing `⏹️` statuses to `⏸️` in `./.ai/artifacts/registry.md`
    - Add new row: `| {uuid} | ⏹️ | {current_timestamp} | {summary} |`

11. **Auto-Open Files (No Confirmation)**
    - Open in editor without asking: `plan.md`, `tasks.md`.

12. **Confirm and Stop**
    - Display: "Plan '{summary}' created with UUID {uuid}. Memory populated via awlab-memory. Files opened in editor."
    - **CRITICAL**: Do NOT execute any implementation, code changes, or task execution.

## Implementation Instructions

When the user asks to implement this plan, **strictly follow the Phase Execution Rules in `02-plan-artifacts.md`**. Do not use any other phase‑execution instructions.