<!-- → authority: 00-meta.md -->
# Offline Cache Protocol — Never Drop Memory When MCP Is Down

## Purpose
When the `awlab-mcp` server (or its agent-recall store) is unreachable, intended
**memory / plan mutations must never be silently dropped**. Queue them to the
offline cache, keep working, and replay them once the server is back. This is
the write-side cache that prevents stale/lost memory.

## The Offline Cache (canonical)
- **File:** `.ai/memory-bank/pending.jsonl` (JSONL — ONE JSON object per line).
- **Why JSONL:** true append (`open("a")`), one line per operation, a torn last
  line never breaks earlier entries (readers skip corrupt lines).
- Appended by two sides:
  1. **Server-side (automatic):** when a store write fails while the server is up
     (`mem_write`/`mem_remove` store down → queued automatically; `task_update`
     DB sync down → a `sync_plan_progress` entry is queued automatically).
  2. **Agent-side (required when MCP is DOWN):** you append with your OWN file tools.

## When Is This Triggered? (agent side)
Treat the server as DOWN only on **connection-level failures** of
`action_call`/`action_help` — e.g. "server not running", timeout, tool not found,
transport error. A normal `{"success": false, ...}` business error is NOT a
server-down; the store is reachable, so just handle/retry the error normally.

## Protocol (MCP down)
1. **Never claim success** for a queued operation — report it as `queued`.
2. Append ONE line to `.ai/memory-bank/pending.jsonl` via your own file
   tools (create the directory silently if needed). Entry shapes:

   - `mem_write` →
     `{"type": "mem_write", "store": "<project|patterns|family_<slug>>", "entities": [...], "observations": [...], "relations": [...]}`
   - `mem_remove` →
     `{"type": "mem_remove", "store": "...", "names": [...], "entities": [...], "deletions": [...], "relations": [...]}`
   - `task_update` →
     `{"type": "update_task_status", "plan_uuid": "...", "task_path": "1.2", "new_status": "[x]"}`
   - If the tasks.md file was already updated but the DB sync failed, use
     `{"type": "sync_plan_progress", "plan_uuid": "...", "updates": [{"task_path": "1.2", "new_status": "[x]"}]}`
3. Keep entries **self-contained** (all params inline, no references to chat
   context), so they replay correctly from any session.
4. Continue the task using your working context; do NOT fabricate that memory
   was persisted.

## Replay (on recovery)
1. When the server is reachable again, call
   `action_call(action="mem_replay", params={"dry_run": true})` to preview the queue.
2. Drain with `action_call(action="mem_replay")` — successful entries are removed;
   failed ones are kept for a later retry.
3. If `mem_replay` is unavailable (old server), replay **manually**: read
   `pending.jsonl` line by line → re-issue each entry as its original
   `action_call` → then delete/clear the file.
4. After replay, refresh orchestration state:
   `action_call(action="ctx_info", params={"mode": "context"})`.

## Guardrails
- ❌ Never silently drop an intended `mem_write` / `mem_remove` / `task_update`.
- ❌ Never hand-edit `pending.jsonl` except to **append** a line (or clear it
  after a successful replay).
- ❌ Never invent `success` for a queued operation.
- ⚠️ A queued `mem_write` may, on replay, re-apply an operation that partially
  succeeded before failing — run `mem_dedupe` after replay if duplicates appear.
- The server's readers tolerate a torn/corrupt tail line — do not worry about a
  partial write while the process dies mid-append.
