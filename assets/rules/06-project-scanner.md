<!-- → authority: 00-meta.md -->
# Project Scanner Rule

## Purpose
Provide a deterministic, framework-aware scanning protocol. All scanning execution logic is delegated to the MCP server's `context_tools`.

## Delegation

When the `plan-creator` skill requires project scanning:

1. **Do NOT manually implement** the Fingerprint Protocol if the `ctx_scan_project` MCP tool is available (registered on the development MCP server at `mcp_server/modules/registration.py`).
2. If the development MCP server is **available**, call:
   ```xml
   <use_mcp_tool>
   <server_name>awlab-mcp</server_name>
   <tool_name>ctx_scan_project</tool_name>
   <arguments>
   {}
   </arguments>
   </use_mcp_tool>
   ```
3. If the development MCP server is **not** available, implement the scan manually using native file-reading tools (see the scanner protocol in the `plan-creator` skill).
4. Store results via awlab-memory `mem_create_entities` + `mem_relate` (see `01-memory-bank.md`).

## Constraint

- Never scan `node_modules/`, `vendor/`, `build/`, `dist/`, `.git/`, or other dependency/output directories.
- If project type cannot be determined by the tool, mark as "Unknown" and ask the user.