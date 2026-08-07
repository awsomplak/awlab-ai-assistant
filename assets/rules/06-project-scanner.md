<!-- → authority: 00-meta.md -->
# Project Scanner Rule

## Purpose
Provide a deterministic, framework-aware scanning protocol. All scanning execution logic is delegated to the `awlab-mcp` server's `ctx_info mode="scan"` action.

## Delegation

When the `plan-creator` skill requires project scanning:

1. **Do NOT manually implement** the Fingerprint Protocol if the `awlab-mcp` server is available (registered at `mcp_server/modules/registration.py`).
2. If the server is **available**, call:
   ```
   action_call(action="ctx_info", params={"mode": "scan"})
   ```
   Pass `force_refresh=true` to bypass the cache.
3. If the server is **not** available, implement the scan manually using native file-reading tools (see the scanner protocol in the `plan-creator` skill).
4. Store results via `mem_write` (entities + relations — see `01-memory-bank.md`).

## Constraint

- Never scan `node_modules/`, `vendor/`, `build/`, `dist/`, `.git/`, or other dependency/output directories.
- If project type cannot be determined by the tool, mark as "Unknown" and ask the user.