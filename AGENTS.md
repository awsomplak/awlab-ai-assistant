# AGENTS.md — read this first (session start)

This project is **AWLab-ID** — an AI-Assisted Development System (rules, workflows, skills, MCP server).
It compiles rules+skills for Cline, VS Code Copilot, Claude Code, and Hermes, and provides a **single MCP server executable** (`dist/bin/awlab-mcp.exe`) exposing **2 tools** — `action_call` + `action_help` — that route **20 actions** (plan, task, memory, graph, context, util, workflow).

## ⚠️ Session-start protocol (mandatory — prevents hallucination)

At the start of **every** session, before touching any code:

1. Read `.ai/memory-bank/context.md` → the `## Current Work & Handoff` section is the single source of truth for what is being worked on **right now** (it is regenerated atomically by `action_call(action="ctx_info", params={"mode": "context"})`). `.ai/memory-bank/environment.md` is static env config only (shell detection, commands).
2. Read the plan registry via `action_call(action="plan_status")` → the **active plan** is the current focus.
3. If an active plan exists, read its `tasks.md` via `action_call(action="task_read", params={"plan_uuid": "...", "format": "structured"})` → next eligible task via `action_call(action="plan_status", params={"plan_uuid": "..."})`.
4. **Never invent task state.** If there is no handoff and no active plan, state that clearly and ask the user what to work on — do not guess, do not fabricate a task, do not "continue" something you cannot see.

## Current work (2026-08-07) — full detail in `context.md`

- **MCP tool consolidation — DONE (plan `mcptool1`, all phases complete)**: the 36 tools across 3 servers were consolidated into a minimal surface for VS Code Copilot efficiency:
  - `action_call(action, params)` dispatcher + `action_help(action)` help tool (CLI `--help` pattern).
  - Single `REGISTRY` dict (action → handler / params / example / doc) generates the tool description, `action_help` output, and SKILL.md — no drift.
  - Single executable `dist/bin/awlab-mcp.exe`; legacy 3-server files deleted.
  - Agentic orchestration: `ctx_info mode="context"` composite atomically regenerates `context.md` (code ↔ memory correlation).
  - Single `task_update` (multi-level paths, transition validation with `valid_targets`, auto-create, atomic rollback, executed/skipped/created trace).
- **Code knowledge graph — incremental rebuild implemented**: `graph_build` now re-extracts only changed files (with the unchanged corpus as resolution context) and merges into the prior graph, so auto-refresh via the `graph_fresh` precondition is ~40x faster than a full rebuild. Verified: incremental output is identical to a full rebuild at the same source state.
- **287 tests pass, lint clean.**
- **Next**: no active plan — user-directed work.
- Reference: `docs/AVAILABLE_TOOLS.md`, `docs/REGISTRY_SCHEMA.md`, `src/mcp_server/registry.py`, `src/mcp_server/modules/dispatcher.py`, `src/mcp_server/helpers/graphify_bridge.py`.
