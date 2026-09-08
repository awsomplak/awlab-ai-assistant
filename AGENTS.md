> **CRITICAL**: Read .ai/awlab-protocol.md first.

# AGENTS.md — AI Agent Guidelines & Operating Rules

This project is **AWLab-AI-Assistant** — an AI-Assisted Development System (rules, workflows, skills, MCP server).
It provides a **single consolidated MCP server** (`awlab-ai-assistant.exe` or via `.venv/Scripts/python.exe`) exposing **2 tools** — `action_call` and `action_help` — routing **24 deterministic actions** across plan, task, memory, graph, context, util, and workflow domains.

---

## 1. ⚠️ Session-Start Protocol (Mandatory)

At the start of **every** session, before modifying or creating code:

1. **Read Project Context:**
   - Read `.ai/memory-bank/context.md`. The `## Current Work & Handoff` section is the single source of truth for ongoing work.
   - If stale or needed, regenerate atomically via:
     `action_call(action="ctx_info", params={"workspace_path": "<workspace_root>", "mode": "context"})`.
   - `.ai/memory-bank/environment.md` contains static environment configuration only.
2. **Check Plan Registry:**
   - Run `action_call(action="plan_status", params={"workspace_path": "<workspace_root>"})` to identify the **active plan**.
3. **Inspect Active Tasks:**
   - If an active plan exists, inspect its tasks:
     `action_call(action="task_read", params={"workspace_path": "<workspace_root>", "plan_uuid": "<plan_uuid>", "format": "structured"})`.
   - Check next eligible task using `plan_status`.
4. **Load User Preferences & Patterns:**
   - Always retrieve active user patterns and conventions at session start:
     `action_call(action="mem_search", params={"workspace_path": "<workspace_root>", "entity_type": "pattern", "store": "patterns", "scope": "all"})`.
   - Immediately apply recovered user conventions to your active workflows.
5. **Never Invent Task State:**
   - If there is no handoff and no active plan, state that clearly to the user and ask for instructions. Never fabricate active tasks or unrecorded state.

---

## 2. Core MCP Operating Rules

- **Workspace Path:** Always pass `workspace_path` (absolute path to project root) for all filesystem/disk actions (`plan_*`, `task_*`, `mem_*`, `graph_*`, `ctx_*`). The server does not perform implicit directory detection.
- **Python Virtual Environment:** In this project, always use the virtual environment binary (`.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on POSIX) for running Python commands and scripts.
- **Self-Documenting Help:** Use `action_help(action="<name>")` to inspect required parameters, types, and defaults before calling unfamiliar actions.
- **Execution Tracing:** Every `action_call` response returns `{success, action, result, executed, skipped}`. Always inspect errors or skipped preconditions if an action fails.

---

## 3. Plan & Task Management

Follow strict plan-first execution for multi-step or non-trivial modifications:

### Plan Lifecycle
- **Create Plan:** Use `action_call(action="plan_create", params={"workspace_path": "...", "summary": "..."})` or `reg_update(type="create", ...)` to register a new plan.
- **Inspect Status:** Call `action_call(action="plan_status", params={"workspace_path": "..."})` to monitor phase progress and gates.
- **Phase Transition:** Mark phases complete via `action_call(action="plan_update", params={"workspace_path": "...", "mode": "mark_phase", "phase_number": <int>})`.
- **Plan Documents:** Read/write `plan.md`, `notes.md`, and `walkthrough.md` via:
  `action_call(action="plan_doc", params={"workspace_path": "...", "plan_uuid": "...", "doc": "plan"|"notes"|"walkthrough", "mode": "read"|"write", "content": "..."})`.
  > **⚠️ TOKEN BURN WARNING:** NEVER call `plan_doc` with `mode="read"` just to understand the plan. It dumps the entire raw file and burns tokens. ALWAYS rely on `ctx_info` for plan context. Only use `plan_doc(mode="read")` when you explicitly need to migrate or rewrite the raw document.
- **Completion Walkthrough:** Always persist a summary walkthrough upon plan or major milestone completion using `plan_doc` (`doc="walkthrough"`).

### Task Tracking
- **Read Tasks:** `action_call(action="task_read", params={"workspace_path": "...", "plan_uuid": "...", "format": "structured"})`.
- **Update Task State Immediately:** Never execute tasks silently. Update status as soon as progress happens:
  - Transition: `[ ]` (pending) → `[/]` (in progress) → `[x]` (completed) or `[—]` (skipped).
  - Update call:
    `action_call(action="task_update", params={"workspace_path": "...", "plan_uuid": "<plan_uuid>", "updates": [{"task_path": "1.1", "new_status": "[/]"}]})`.

---

## 4. Memory Management & User Patterns

AWLab-AI-Assistant maintains an isolated, type-safe entity-observation memory store (SQLite backed, keyed by `project_id`).

### Context & Discovery
- **Search Memory:**
  `action_call(action="mem_search", params={"workspace_path": "...", "query": "..."})` — hybrid BM25 + dense search.
  Use `entity_type="pattern", store="patterns", scope="all"` for conventions, or filter by `decision`, `concept`, `bug`, etc.
- **Inspect Entities:**
  - Read specific node: `action_call(action="mem_read", params={"workspace_path": "...", "node": "..."})`.
  - Audit entity inventory: `action_call(action="mem_list_entities", params={"workspace_path": "...", "limit": 100})`.
  - Deduplicate entities: `action_call(action="mem_dedupe", params={"workspace_path": "...", "name": "..."})`.

### Pattern Capture & Observation
- **Observation-First (`mem_observe`):** When encountering user preferences, corrections ("Don't do X, do Y"), or repeated command behavior, record them into `.ai/memory-bank/observations.jsonl`:
  ```json
  action_call(action="mem_observe", params={
    "workspace_path": "...",
    "observations": [{
      "signature": "<unique_key>",
      "value": "<observed convention or preference>",
      "source": "explicit" | "corrected" | "behavioral"
    }]
  })
  ```
  The deterministic baking pipeline converts observations into candidates without LLM hallucination.
- **Direct Write (`mem_write`):** Use for confirmed, explicit conventions or relating entities:
  `action_call(action="mem_write", params={"workspace_path": "...", "entities": [...], "observations": [...]})`.
- **Offline Cache (`mem_replay`):** If MCP mutations occurred while offline, replay `.ai/memory-bank/pending.jsonl` using `action_call(action="mem_replay", params={"workspace_path": "..."})`.

---

## 5. Code Knowledge Graph (Codebase Comprehension)

AWLab-AI-Assistant maintains an AST-based structural code knowledge graph in `.ai/codegraph/` (`graph.json` + interactive `graph.html`).

### Grounding Before Modifying
Never guess dependencies, call chains, or symbol hierarchies. Use the code graph:
- **Freshness Check:** Verify graph state with `action_call(action="graph_status", params={"workspace_path": "..."})`.
- **Build / Freshen:**
  `action_call(action="graph_build", params={"workspace_path": "...", "background": false})`
  Incremental rebuilds extract only changed files against the existing AST context (~40x faster than full builds).
- **Search Symbols:**
  `action_call(action="graph_query", params={"workspace_path": "...", "query": "<symbol_name>"})`
  Indexes file, function, class, and component labels. Automatically falls back to whole-word source search if no AST node matches.
- **Explain Nodes:**
  `action_call(action="graph_explain", params={"workspace_path": "...", "node": "<symbol_or_id>"})`
  Provides node declaration details, direct inbound/outbound relationships, and correlated memory entities.
- **Trace Relationships:**
  `action_call(action="graph_path", params={"workspace_path": "...", "a": "<symbol_a>", "b": "<symbol_b>"})`
  Finds shortest symbol-level or module-level dependency paths between components.

---

## 6. Full Orchestration (`ctx_info`)

Use `ctx_info` for consolidated context management:
- **Composite Context (`mode="context"`):** Atomically gathers active plan, next tasks, code graph state, and relevant memory, while regenerating `.ai/memory-bank/context.md`. Optional `query` parameter scopes relevance.
- **Snapshot (`mode="snapshot"`):** Fast read of active plan + baked patterns + project ID.
- **Scan (`mode="scan"`):** Inspect detected frameworks, build systems, and runtime environments.
- **Suggest (`mode="suggest"`):** Suggest candidate source files for a given task description using graph and memory correlation.

