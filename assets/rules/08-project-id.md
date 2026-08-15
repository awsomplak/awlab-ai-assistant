<!-- → authority: 00-meta.md --># Project ID Auto-Creation (check-and-create)

## Purpose
A stable per-project identifier (`.ai/project-id`) that drives memory isolation via
`AGENT_RECALL_SLUG`. Without it, memory falls to the user-wide GLOBAL DB
(`~/.awlab-id/agent-memory/memory/memory.db`) instead of the per-project DB — so the
project-id MUST exist before ANY `mem_*` / plan operation.

## STRICT FIRST-CALL RULE (mandatory)
On the **first response of every session** (before the AI Agent processes the user's
prompt / before ANY `mem_*` / plan / task operation), call the server-side checker:

```
action_call(action="project_id", params={"workspace_path": "<project root>"})
```

The action **checks AND auto-creates in one call** — no long flow:
- `.ai/project-id` exists → returns it (`action: check`, no change).
- Missing → derives the sanitized directory-name slug (lowercase, non-alphanumerics →
  `_`) and writes `.ai/project-id` (`action: create`), so memory is project-isolated.

Do NOT hand-create the file. Do NOT skip this call. A missing project-id is a bug that
puts memory in the global DB.

## Notes
- `force_regenerate=true` re-creates the id (rare — only when the slug is wrong).
- The id is read from `.ai/project-id` by the wrapper (AGENT_RECALL_SLUG) and by
  `awlab-ai-assistant` for per-project DB isolation.
