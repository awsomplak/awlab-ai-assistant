"""
Context builder — assemble and atomically write ``.ai/memory-bank/context.md``.

``ctx_info mode="context"`` returns the full orchestration context (plan + next
task + code + memory) as JSON to the agent AND atomically replaces ``context.md``
with a human-readable snapshot of the SAME payload, so the file is always the
current state (session-start for the agent, follow-up view for the user in VS Code).

Contract:
- ONE fixed-name file (``context.md``) — always overwritten, never appended.
- Atomic replace (write temp + ``os.replace``) — the reader never sees a partial
  file; a crash mid-write leaves the previous complete version intact.
- Deterministic, no LLM. Never raises — a failed write is a logged no-op.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from .logger import logger

# Fixed name so the file is always overwritten (no unbounded new files).
CONTEXT_FILE = "context.md"


def context_path(workspace_path: str | Path) -> Path:
    """Absolute path to the project's context.md."""
    return settings.get_memory_bank_dir(workspace_path=workspace_path) / CONTEXT_FILE


def _fmt_plan(plan: dict | None) -> list[str]:
    """Render the plan section of context.md."""
    if not plan or not isinstance(plan, dict):
        return ["_No active plan._"]
    registry = plan.get("registry") or {}
    active = (registry.get("active") or [{}])[0] if isinstance(registry, dict) else {}
    lines: list[str] = []
    if active.get("summary"):
        lines.append(f"- **Active plan:** {active.get('summary')}  (`{active.get('uuid')}`)")
    next_task = plan.get("next_task")
    if isinstance(next_task, dict) and next_task.get("task"):
        lines.append(f"- **Next task:** {next_task['task']}")
    elif isinstance(next_task, dict) and next_task.get("description"):
        lines.append(f"- **Next task:** {next_task['description']}")
    if isinstance(next_task, dict) and next_task.get("phase_number") is not None:
        lines.append(f"- **Phase:** {next_task.get('phase_number')}")
    completable = plan.get("completable")
    if isinstance(completable, dict):
        lines.append(f"- **Completable:** {completable.get('completable')}")
    return lines or ["_No active plan._"]


def _fmt_code(code: dict | None, query: str) -> list[str]:
    """Render the code-graph section of context.md."""
    if not code or not isinstance(code, dict):
        return []
    results = code.get("results") or []
    if not results:
        return [f"_No code results for query '{query}'._"] if query else ["_No code context._"]
    lines = [f"- Query `{query}` → {len(results)} node(s):"]
    for r in results[:8]:
        label = r.get("label") or r.get("id") or ""
        src = r.get("source_file") or ""
        lines.append(f"  - `{label}`" + (f"  ({src})" if src else ""))
    return lines


def _fmt_memory(mem: dict | None) -> list[str]:
    """Render the memory section of context.md."""
    if not mem or not isinstance(mem, dict):
        return []
    if mem.get("mode") == "inventory":
        total = mem.get("total_entities", 0)
        if total == 0:
            return ["_Memory is empty — nothing stored yet. Use `mem_write` to save knowledge._"]
        lines = [f"- **Memory inventory:** {total} entit(y/ies) across {len(mem.get('by_type') or {})} type(s):"]
        by_type = mem.get("by_type") or {}
        lines.append(f"  - Types: {', '.join(f'{k} ({v})' for k, v in sorted(by_type.items()))}")
        for ent in (mem.get("entities") or [])[:10]:
            name = ent.get("name") or ""
            etype = ent.get("entityType") or "?"
            n_obs = ent.get("observation_count", 0)
            lines.append(f"  - `{name}` ({etype}, {n_obs} obs)")
        return lines
    data = mem.get("data") if isinstance(mem, dict) else None
    results = data if isinstance(data, list) else []
    if not results:
        return ["_No related memory._"]
    lines = [f"- {len(results)} related memory entit(y/ies):"]
    for r in results[:8]:
        name = r.get("name") or r.get("entity_name") or ""
        obs = r.get("observations") or []
        snippet = obs[0][:80] + ("…" if obs and len(obs[0]) > 80 else "") if obs else ""
        lines.append(f"  - `{name}`" + (f" — {snippet}" if snippet else ""))
    return lines


def _fmt_plan_doc(plan_doc: dict | None) -> list[str]:
    """Render the parsed plan.md section (approach + preferences)."""
    if not plan_doc or not isinstance(plan_doc, dict):
        return ["_No plan.md._"]
    lines: list[str] = []
    overview = (plan_doc.get("overview") or "").strip()
    if overview:
        snippet = overview.replace("\n", " ")[:200]
        lines.append(f"- **Overview:** {snippet}{'…' if len(snippet) == 200 else ''}")
    approach = plan_doc.get("approach") or []
    if approach:
        lines.append(f"- **Approach ({len(approach)}):** {approach[0][:150]}")
    prefs = (plan_doc.get("sections") or {}).get("user_preferences_learned", {}).get("bullets", [])
    if prefs:
        lines.append(f"- **Preferences ({len(prefs)}):** {prefs[0][:150]}")
    return lines or ["_plan.md parsed (no sections)._ "]


def _fmt_notes_doc(notes_doc: dict | None) -> list[str]:
    """Render the parsed notes.md section (decisions / constraints / risks)."""
    if not notes_doc or not isinstance(notes_doc, dict):
        return ["_No notes.md._"]
    lines: list[str] = []
    for label, key in (("Decisions", "key_decisions"), ("Constraints", "constraints"), ("Risks", "risks")):
        items = notes_doc.get(key) or []
        if items:
            lines.append(f"- **{label} ({len(items)}):** {items[0][:150]}")
    return lines or ["_notes.md parsed (no sections)._ "]


def _fmt_walkthrough_doc(walkthrough_doc: dict | None) -> list[str]:
    """Render the walkthrough.md section (post-completion summary)."""
    if not walkthrough_doc or not isinstance(walkthrough_doc, dict):
        return ["_No walkthrough.md._"]
    if not walkthrough_doc.get("success"):
        return ["_No walkthrough.md._"]
    content = (walkthrough_doc.get("content") or "").strip()
    if not content:
        return ["_walkthrough.md is empty._"]
    # Show the first 300 chars as a snippet
    snippet = content.replace("\n", " ")[:300]
    return [f"- **Walkthrough:** {snippet}{'…' if len(content) > 300 else ''}"]


def _fmt_patterns(patterns: list | None, candidates: list | None) -> list[str]:
    """Render the baked-patterns section of context.md (candidates first)."""
    lines: list[str] = []
    cands = candidates or []
    if cands:
        lines.append(f"- **New pattern candidates ({len(cands)})** — told once, act per the gate:")
        for c in cands[:8]:
            sig = c.get("signature") or ""
            val = (c.get("value") or "")[:100]
            conf = c.get("confidence")
            suffix = f"  (conf {conf})" if conf is not None else ""
            lines.append(f"  - `{sig}` — {val}{suffix}")
    pats = patterns or []
    if pats:
        lines.append(f"- **Baked patterns ({len(pats)}):**")
        for p in pats[:8]:
            sig = p.get("signature") or ""
            val = (p.get("value") or "")[:100]
            lines.append(f"  - `{sig}` — {val}")
    if not lines:
        return ["_No baked patterns yet — observations accumulate via `mem_observe` / hooks._"]
    return lines


def build_context_md(
    workspace_path: str | Path,
    plan: dict | None = None,
    code: dict | None = None,
    memory: dict | None = None,
    query: str = "",
    plan_doc: dict | None = None,
    notes_doc: dict | None = None,
    walkthrough_doc: dict | None = None,
    patterns: list | None = None,
    pattern_candidates: list | None = None,
) -> str:
    """Assemble the full context.md content from the orchestration composite."""
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Project Context",
        "",
        f'> Generated by `ctx_info mode="context"` at {now}. Read-only snapshot of '
        "the current orchestration state — atomic-replaced on every refresh. "
        'Follow up here or via `action_call(action="ctx_info", params={"mode": "context"})`.',
        "",
        "## Plan",
        "",
        *_fmt_plan(plan),
        "",
        "## Plan Document (plan.md)",
        "",
        *_fmt_plan_doc(plan_doc),
        "",
        "## Notes (notes.md)",
        "",
        *_fmt_notes_doc(notes_doc),
        "",
        "## Walkthrough",
        "",
        *_fmt_walkthrough_doc(walkthrough_doc),
        "",
        "## Code",
        "",
        *_fmt_code(code, query),
        "",
        "## Memory",
        "",
        *_fmt_memory(memory),
        "",
        "## Patterns",
        "",
        *_fmt_patterns(patterns, pattern_candidates),
        "",
    ]
    content = "\n".join(lines)
    # Truncate to protect LLM context window (approx 7.5k tokens)
    if len(content) > 30000:
        content = content[:30000] + "\n\n...[TRUNCATED FOR CONTEXT WINDOW]..."
    return content


def write_context_md_atomic(workspace_path: str | Path, content: str) -> bool:
    """Atomically replace context.md (write temp + os.replace). Never raises."""
    target = context_path(workspace_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".context.md.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        logger.warning(f"context.md write failed: {e}")
        return False


def materialize_context(
    workspace_path: str | Path,
    plan: dict | None = None,
    code: dict | None = None,
    memory: dict | None = None,
    query: str = "",
    plan_doc: dict | None = None,
    notes_doc: dict | None = None,
    walkthrough_doc: dict | None = None,
    patterns: list | None = None,
    pattern_candidates: list | None = None,
) -> dict:
    """Build + atomically write context.md. Returns ``{success, path, changed}``.

    Used by ``ctx_info mode="context"`` so the file is ALWAYS written (never
    optional) and always reflects what the agent just received.
    """
    _ensure_protocol_and_agents_md(workspace_path)

    content = build_context_md(
        workspace_path,
        plan=plan,
        code=code,
        memory=memory,
        query=query,
        plan_doc=plan_doc,
        notes_doc=notes_doc,
        walkthrough_doc=walkthrough_doc,
        patterns=patterns,
        pattern_candidates=pattern_candidates,
    )
    ok = write_context_md_atomic(workspace_path, content)
    return {
        "success": ok,
        "path": str(context_path(workspace_path)),
        "changed": ok,
        "bytes": len(content),
    }


AWLAB_PROTOCOL_MD = """\
# AWLab-AI-Assistant Protocol — read this first (session start)

This project uses **AWLab-AI-Assistant** — an AI-Assisted Development System.

## ⚠️ Session-start protocol (mandatory — prevents hallucination)

At the start of **every** session, before touching any code:

1. Read `.ai/memory-bank/context.md` → the `## Current Work & Handoff` section is the single source of truth for what
   is being worked on **right now** (it is regenerated atomically by `action_call(action="ctx_info",
   params={"mode": "context"})`).
   `.ai/memory-bank/environment.md` is static env config only (shell detection, commands).
2. Read the plan registry via `action_call(action="plan_status")` → the **active plan** is the current focus.
3. If an active plan exists, read its `tasks.md` via `action_call(action="task_read", params={"plan_uuid": "...",
   "format": "structured"})` → next eligible task via `action_call(action="plan_status", params={"plan_uuid": "..."})`.
4. **Load User Patterns:** Always retrieve user preferences at the start of a session by running
   `action_call(action="mem_search", params={"entity_type": "pattern"})` and apply them to your workflow.
5. **Never invent task state.** If there is no handoff and no active plan, state that clearly and ask the user what
   to work on — do not guess, do not fabricate a task, do not "continue" something you cannot see.
"""


def _ensure_protocol_and_agents_md(workspace_path: str | Path) -> None:
    try:
        wspath = Path(workspace_path).resolve()
        ai_dir = wspath / ".ai"
        ai_dir.mkdir(parents=True, exist_ok=True)

        proto_path = ai_dir / "awlab-protocol.md"
        if not proto_path.exists():
            proto_path.write_text(AWLAB_PROTOCOL_MD, encoding="utf-8")

        agents_md_path = wspath / "AGENTS.md"
        magic_line = "> **CRITICAL**: Read .ai/awlab-protocol.md first.\n"
        if agents_md_path.exists():
            content = agents_md_path.read_text(encoding="utf-8")
            if "awlab-protocol.md" not in content:
                agents_md_path.write_text(magic_line + "\n" + content, encoding="utf-8")
        else:
            agents_md_path.write_text(magic_line + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to bootstrap awlab protocol: {e}")


def read_context_md(workspace_path: str | Path) -> str | None:
    """Read the current context.md content, or None if absent."""
    path = context_path(workspace_path)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def context_as_json(workspace_path: str | Path) -> dict:
    """Return the current context.md as a JSON object (for the agent)."""
    content = read_context_md(workspace_path)
    if content is None:
        return {"success": False, "error": "context.md not generated yet"}
    return {"success": True, "file": CONTEXT_FILE, "content": content, "json": json.dumps({"content": content})}
