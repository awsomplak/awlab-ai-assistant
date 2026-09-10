<!-- → authority: 00-meta.md -->
# Project Scanner Rule

## Purpose
Provide a deterministic, framework-aware scanning protocol. All scanning execution logic is delegated to the `AWLab-AI-Assistant` server's `ctx_info mode="scan"` action.

## Delegation

When the `plan-creator` skill requires project scanning:

1. **Do NOT manually implement** the Fingerprint Protocol if the `AWLab-AI-Assistant` server is available (registered at `mcp_server/modules/registration.py`).
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

## Graph-First Code Comprehension (mandatory before reading source files)

When you need to LOCATE a symbol / method / class / caller or understand how code
is connected, navigate the **code knowledge graph** before opening files. The graph
is AST-accurate and auto-freshens, so it answers most "where is X / who calls X"
questions in one call:

1. **`action_call(action="graph_query", ...)`** — find the node(s) for the symbol
   (`query="<name>"`). It also returns freshness metadata; a missing graph is built
   automatically (`graph_fresh` precondition).
2. **`action_call(action="graph_explain", ...)`** — get a hit's declaration plus its
   direct neighbours/callers (`node=<label>`).
3. **`action_call(action="graph_path", ...)`** — shortest dependency path between two
   symbols (`a`, `b`).
4. Only then read the 1-3 most relevant files (`view_file`/`read_file`) — bounded by
   the 5-file turn budget in `03-token-strategies.md`.

**Fallback (only when the graph is genuinely unhelpful):** a query dead-ends, the
project is not yet indexed, or you need EXACT literal text (a specific string,
comment, or config value). In that case use `grep_search`/source scan, then re-query
or read the file.

**Anti-pattern:** reading or grepping whole files to discover a symbol the code graph
can locate by name in one call — that is exactly the token waste the graph exists to
prevent.