"""
HOOK_ADAPTERS — per-agent input normalizers + output serializers.

Mirrors the REGISTRY pattern: one table (agent → {normalize, serialize}) is the single
source of truth for how each host's hook payload maps to the internal :class:`HookEvent`
and how the internal result maps back to the host's response shape.

Anti-loop contract is enforced here:
- ``prompt`` events are the ONLY ones that may inject context.
- ``tool`` / ``pre_tool`` / ``stop`` / ``session`` / ``subagent`` are observer-only in
  the serializer (empty output) except the explicit block verdict.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .hook_event import HookEvent

# ── Event-name → kind ───────────────────────────────────────────────────────


def _kind_prompt(event: str) -> str:
    return "prompt" if event.lower() in ("userpromptsubmit", "pre_llm_call") else "tool"


def _kind_claude(event: str) -> str:
    e = event.lower()
    if e == "userpromptsubmit":
        return "prompt"
    if e in ("pretooluse", "pre_tool_call"):
        return "pre_tool"
    if e in ("posttooluse", "post_tool_call"):
        return "tool"
    if e in ("subagentstart", "subagentstop", "subagent_start", "subagent_stop"):
        return "subagent"
    if e in ("sessionstart", "sessionend", "on_session_start", "on_session_end"):
        return "session"
    return "stop"  # Stop / post_llm_call / anything else → bake at turn end


def _kind_generic(event: str) -> str:
    e = event.lower()
    if "prompt" in e or "llm_call" in e and "pre" in e:
        return "prompt"
    if "pre" in e and "tool" in e:
        return "pre_tool"
    if "tool" in e:
        return "tool"
    if "subagent" in e:
        return "subagent"
    if "session" in e:
        return "session"
    return "stop"


def kind_for_event(agent: str, event: str) -> str:
    """Map a host event name to a HookEvent kind (anti-loop dispatch)."""
    if agent == "claude":
        return _kind_claude(event)
    if agent == "copilot":
        return _kind_prompt(event) if event.lower() == "userpromptsubmit" else _kind_generic(event)
    return _kind_generic(event)


# ── Adapter: Hermes ──────────────────────────────────────────────────────────


def _normalize_hermes(raw: dict[str, Any], event: str) -> HookEvent:
    extra = raw.get("extra") or {}
    ev = HookEvent(
        agent="hermes",
        event=event,
        kind=kind_for_event("hermes", event),
        project_path=str(raw.get("cwd") or ""),
        session_id=str(raw.get("session_id") or ""),
        user_message=str(extra.get("user_message") or ""),
        tool_name=str(raw.get("tool_name") or ""),
        tool_input=raw.get("tool_input") or {},
        tool_result=str(extra.get("result") or ""),
        assistant_response=str(extra.get("assistant_response") or ""),
        subagent=(
            {
                "goal": str(extra.get("child_goal") or ""),
                "role": str(extra.get("child_role") or ""),
                "child_id": str(extra.get("child_subagent_id") or ""),
            }
            if event.startswith("subagent")
            else {}
        ),
        extra=extra,
    )
    # Claude-Code env var fallback for project path (also present on Hermes shells).
    if not ev.project_path:
        ev.project_path = os.environ.get("CLAUDE_PROJECT_DIR", "")
    return ev


def _serialize_hermes(event: str, result: dict[str, Any]) -> str:
    kind = kind_for_event("hermes", event)
    if kind == "prompt":
        context = result.get("context")
        if context:
            return json.dumps({"context": context})
        return "{}"
    if kind == "pre_tool":
        block = result.get("block")
        if block:
            return json.dumps({"action": "block", "message": block})
        return "{}"
    return "{}"  # observer-only


# ── Adapter: Claude Code ─────────────────────────────────────────────────────


def _normalize_claude(raw: dict[str, Any], event: str) -> HookEvent:
    ti = raw.get("tool_input") or {}
    return HookEvent(
        agent="claude",
        event=event,
        kind=kind_for_event("claude", event),
        project_path=str(raw.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")),
        session_id=str(raw.get("session_id") or ""),
        user_message=str(raw.get("prompt") or ""),
        tool_name=str(raw.get("tool_name") or ""),
        tool_input=ti if isinstance(ti, dict) else {"command": str(ti)},
        tool_result=str(raw.get("tool_response") or ""),
        subagent=raw.get("subagent") or {},
        extra=raw.get("extra") or {},
    )


def _serialize_claude(event: str, result: dict[str, Any]) -> str:
    kind = kind_for_event("claude", event)
    if kind == "prompt":
        context = result.get("context")
        if context:
            return json.dumps({"decision": "allow", "additionalContext": context})
        return json.dumps({"decision": "allow"})
    if kind == "pre_tool":
        block = result.get("block")
        if block:
            return json.dumps({"decision": "block", "reason": block})
        return "{}"
    return "{}"  # observer-only


# ── Adapter: Copilot ─────────────────────────────────────────────────────────


def _normalize_copilot(raw: dict[str, Any], event: str) -> HookEvent:
    ws = raw.get("workspace") or raw.get("workspace_path") or raw.get("uri") or ""
    if isinstance(ws, dict):
        ws = str(ws.get("path") or ws.get("fsPath") or "")
    return HookEvent(
        agent="copilot",
        event=event,
        kind=kind_for_event("copilot", event),
        project_path=str(ws or raw.get("cwd") or ""),
        session_id=str(raw.get("session_id") or ""),
        user_message=str(raw.get("prompt") or raw.get("message") or ""),
        tool_name=str(raw.get("tool_name") or ""),
        tool_input=raw.get("tool_input") or {},
        tool_result=str(raw.get("result") or ""),
        subagent=raw.get("subagent") or {},
        extra=raw.get("extra") or {},
    )


def _serialize_copilot(event: str, result: dict[str, Any]) -> str:
    kind = kind_for_event("copilot", event)
    if kind == "prompt":
        context = result.get("context")
        if context:
            return json.dumps({"decision": "allow", "additionalContext": context})
        return json.dumps({"decision": "allow"})
    return "{}"  # observer-only


# ── Adapter: Cline ───────────────────────────────────────────────────────────


def _normalize_cline(raw: dict[str, Any], event: str) -> HookEvent:
    return HookEvent(
        agent="cline",
        event=event,
        kind=kind_for_event("cline", event),
        project_path=str(raw.get("cwd") or raw.get("workspace_path") or ""),
        session_id=str(raw.get("session_id") or raw.get("task_id") or ""),
        user_message=str(raw.get("message") or ""),
        tool_name=str(raw.get("tool_name") or ""),
        tool_input=raw.get("tool_input") or {},
        tool_result=str(raw.get("result") or ""),
        subagent=raw.get("subagent") or {},
        extra=raw.get("extra") or {},
    )


def _serialize_cline(event: str, result: dict[str, Any]) -> str:
    # Cline built-in subagents can't call MCP — observer-only, no relay.
    return "{}"


# ── Registry ────────────────────────────────────────────────────────────────

Adapter = tuple[Callable[[dict[str, Any], str], HookEvent], Callable[[str, dict[str, Any]], str]]

HOOK_ADAPTERS: dict[str, Adapter] = {
    "hermes": (_normalize_hermes, _serialize_hermes),
    "claude": (_normalize_claude, _serialize_claude),
    "copilot": (_normalize_copilot, _serialize_copilot),
    "cline": (_normalize_cline, _serialize_cline),
}


def normalize_payload(agent: str, event: str, raw: dict[str, Any]) -> HookEvent | None:
    """Normalize a raw host payload into a HookEvent. Returns None for unknown agent."""
    adapter = HOOK_ADAPTERS.get(agent)
    if adapter is None:
        return None
    return adapter[0](raw, event)


def serialize_output(agent: str, event: str, result: dict[str, Any]) -> str:
    """Serialize an internal result to the host's response shape."""
    adapter = HOOK_ADAPTERS.get(agent)
    if adapter is None:
        return "{}"
    return adapter[1](event, result)
