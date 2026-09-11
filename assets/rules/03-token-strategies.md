<!-- → authority: 00-meta.md -->
# Token Optimization Strategies

## Core Principles

- Parse text files, don't execute shell commands for discovery
- Load files on-demand, not eagerly at startup
- Keep filenames short, merge content when logical
- Use structured formats that are parseable with minimal tokens
- All paths are relative to current project root

## Specific Rules

### Registry First

- Always read `./.ai/artifacts/registry.md` to find plans
- Never `ls`, `find`, or `tree` the `./.ai/artifacts/` directory
- Parse the markdown table directly for UUID discovery
- **After reading the registry, remember the active UUID; do not re-read the registry unless switching plans or checking status.**

### Context Retrieval

- `AWLab-AI-Assistant` memory actions inject relevant memories on demand. Do not load memory files manually.
- Use `action_call(action="mem_search", ...)` before starting tasks to retrieve context.
- The only files that may be read are `./.ai/memory-bank/environment.md` (shell detection) and `./.ai/memory-bank/context.md` (read via `ctx_info mode="context"`).

### Context Budget

Different AI agents have wildly different context window capacities (ranging from 8k to 1M+ tokens). Do **not** attempt to "mentally track" an arbitrary point score across turns. Instead, use natural heuristics and physical checkpoints:

- **Heuristic Tracking**: Monitor your own performance. If you find yourself repeatedly searching the same files, looping over previous decisions, or forgetting instructions from earlier in the session, your context is overwhelmed.
- **Session Checkpoints**: If you detect context decay, explicitly recommend: "⚠️ Session context appears saturated. Use `mem_write` to save state, then start a fresh session."
- **Hard Limit**: If you are repeatedly failing a complex task, STOP and force a checkpoint: "🛑 Context limit reached. Saving state via `mem_write`. Start a new session."

### Post-Compact Recovery Protocol

When a session compacts (or you detect context decay), recover deterministically instead of rebuilding from scratch:

1. **Restore minimal state** — call `action_call(action="ctx_info", params={"mode": "compact"})`. It returns only what you need to resume: active plan + next task, project/family store ids, and `session.tool_calls_this_session` (per-worker counter; resets on worker restart = fresh-session signal).
2. **Pull only relevant memory** — `action_call(action="mem_search", params={"query": "<current task>", "limit": 3})`. Use the returned `total_matches` / `truncated` to know if results were cut; raise `limit` only if needed.
3. **Resume from the checkpoint** — continue the active plan's next task. Do NOT re-read the full plan or memory; the compact snapshot + 3-result search is enough to continue.

This turns compaction from a "context loss event" into a "context checkpoint". The server cannot detect compaction or push state — recovery is cheap and structured by design.

### Proactive Cognitive Cache Protocol

To prevent context bloat and token waste, you MUST NOT re-read files that have already been loaded or whose content is available in your current chat history unless:
1. The file was modified by a command or user action in the current turn.
2. The user explicitly requests you to re-read or refresh the file contents.
3. You explicitly explain in your thought block why the re-read is required (e.g., severe token shift or context loss).

Otherwise, retrieve file structures and key details from your conversation context/history rather than calling `view_file` again. Never spam the chat with "⚠️ Re-reading {file}" warnings; handle checks silently in your thought process.

### File Optimization

- Short filenames: `brief.md` over `projectBrief.md` (only applies to the single exception `environment.md`)
- Combined content: Merge related information into fewer files
- Avoid verbose explanations in generated files
- Use compact table formats for data
- Do **not** create `notes.md` unless there are significant constraints or risks
- **Anti-Explosion Reading Protocol (Strict 5-File Turn Budget)**: To preserve processing performance, prevent API connection interruptions (`Invalid API Response`), and avoid context-bloat, you MUST NOT read more than **5 files** in a single execution turn.
  - **Absolute Parallel Limit**: Never invoke `view_file` on more than 5 files concurrently in the same turn.
  - **Incremental Search Processing**: If a `grep_search` or directory listing returns multiple matches, you **MUST NOT** immediately bulk-read all matched files. Instead:
    1. Scan the search match snippets/lines directly from the search output.
    2. Identify and rank the top **1 to 3 most relevant files**.
    3. Read only those top 1-3 files in the current turn.
    4. Only read additional files in subsequent turns if absolutely necessary after analyzing the initial set.
  - **Graph-First, High-Fidelity Search**: To locate a SYMBOL / method / class / caller,
    call the code graph FIRST (`graph_query`, then `graph_explain`/`graph_path` via
    `action_call`) — it is AST-accurate, auto-freshens, and answers "where is X" in one
    call. Use `grep_search`/`mem_search` only for EXACT literal text, comments, config
    values, or when the graph is missing and a query dead-ends. Never grep/read whole
    files to discover a symbol the graph can find by name.

### Compact Rules & Minimal Action Profile (Small Local Models)
To prevent small local models (1.5B–3B parameters) from suffering context window failures, slow outputs, or tool-timeout loops:
1. **Pre-Compacted Rules Profile**: Rather than feeding full descriptive rulesets to resource-constrained models, load a pre-compacted, stripped-down `.clinerules` containing only command syntax and trigger keywords.
2. **Minimal Action Profile**: If the full rules are loaded, the model should immediately adopt the Minimal Action Profile:
   - **Zero Prose**: Omit conversational chat, introductory pleasantries, and lengthy summaries.
   - **Pure Actionable Commands**: Output thoughts in brief, 1-line bullet points, and go straight to native tool invocations.
   - **Lazy Loading Lockout**: Lock out all non-essential file operations, viewing strictly only active tasks and immediate target files.

## Anti-Patterns to Avoid

- ❌ Scanning the `./.ai/artifacts/` directory to find plans (parse `registry.md` instead)
- ❌ Creating separate files for small amounts of related content
- ❌ Reading entire plan files when only the task list is needed
- ❌ Mixing artifacts between different projects
- ❌ Re-reading the registry multiple times in one session when the active plan hasn’t changed
- ❌ Calling `plan_doc(mode="read")` to gather general plan context (it dumps the massive raw file and burns tokens). ALWAYS use `ctx_info` instead unless explicitly rewriting the plan.
