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
    # Note: parentheses are explicit to prevent operator-precedence bugs
    # (`and` binds tighter than `or` in Python).
    if "prompt" in e or ("llm_call" in e and "pre" in e):
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


def _kind_antigravity(event: str) -> str:
    e = event.lower()
    if e in ("preinvocation", "prompt", "userpromptsubmit"):
        return "prompt"
    if e in ("pretooluse", "pre_tool_call"):
        return "pre_tool"
    if e in ("posttooluse", "post_tool_call"):
        return "tool"
    if e in ("postinvocation", "stop"):
        return "stop"
    return _kind_generic(event)


def kind_for_event(agent: str, event: str) -> str:
    """Map a host event name to a HookEvent kind (anti-loop dispatch)."""
    if agent == "claude":
        return _kind_claude(event)
    if agent == "copilot":
        return _kind_prompt(event) if event.lower() == "userpromptsubmit" else _kind_generic(event)
    if agent == "antigravity":
        return _kind_antigravity(event)
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


# ── Adapter: Antigravity ────────────────────────────────────────────────────


def _normalize_antigravity(raw: dict[str, Any], event: str) -> HookEvent:
    ws_paths = raw.get("workspacePaths") or []
    project_path = str(ws_paths[0]) if ws_paths and isinstance(ws_paths, list) else str(raw.get("cwd") or "")
    tool_call = raw.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or raw.get("tool_name") or "")
    tool_input = tool_call.get("args") or raw.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"command": str(tool_input)}
    return HookEvent(
        agent="antigravity",
        event=event,
        kind=kind_for_event("antigravity", event),
        project_path=project_path,
        session_id=str(raw.get("conversationId") or raw.get("session_id") or ""),
        turn_id=str(raw.get("stepIdx") or ""),
        user_message=str(raw.get("prompt") or raw.get("userMessage") or ""),
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=str(raw.get("error") or raw.get("tool_response") or ""),
        extra=raw,
    )


def _serialize_antigravity(event: str, result: dict[str, Any]) -> str:
    kind = kind_for_event("antigravity", event)
    if kind == "pre_tool":
        block = result.get("block")
        if block:
            return json.dumps({"decision": "deny", "reason": block})
        return json.dumps({"decision": "allow"})
    if kind == "stop":
        block = result.get("block")
        if block:
            return json.dumps({"decision": "continue", "reason": block})
        return "{}"
    if kind == "prompt":
        context = result.get("context")
        if context:
            return json.dumps({"injectSteps": [{"ephemeralMessage": context}]})
        return "{}"
    return "{}"  # observer-only


# ── Adapter: OpenCode ────────────────────────────────────────────────────────


def _normalize_opencode(raw: dict[str, Any], event: str) -> HookEvent:
    """Normalize an OpenCode hook payload.

    OpenCode mirrors the AGENTS.md / opencode.json hook format; payloads
    typically carry ``cwd``, ``session_id``, ``tool_name``, ``tool_input``,
    and ``result``. Falls back to generic field names when missing.
    """
    ws = raw.get("workspace_path") or raw.get("cwd") or ""
    return HookEvent(
        agent="opencode",
        event=event,
        kind=kind_for_event("opencode", event),
        project_path=str(ws),
        session_id=str(raw.get("session_id") or ""),
        user_message=str(raw.get("prompt") or raw.get("message") or ""),
        tool_name=str(raw.get("tool_name") or ""),
        tool_input=raw.get("tool_input") or {},
        tool_result=str(raw.get("result") or raw.get("tool_result") or ""),
        subagent=raw.get("subagent") or {},
        extra=raw.get("extra") or {},
    )


def _serialize_opencode(event: str, result: dict[str, Any]) -> str:
    """Serialize an internal result to OpenCode's response shape.

    OpenCode currently uses the same JSON stdout shape as Copilot for
    prompt-injection; pre_tool deny uses a ``{"block": true, "reason": ...}``
    convention. Observer-only for tool/stop/session/subagent events.
    """
    kind = kind_for_event("opencode", event)
    if kind == "prompt":
        context = result.get("context")
        if context:
            return json.dumps({"context": context})
        return "{}"
    if kind == "pre_tool":
        block = result.get("block")
        if block:
            return json.dumps({"block": True, "reason": block})
        return "{}"
    return "{}"  # observer-only


# ── Registry ────────────────────────────────────────────────────────────────

Adapter = tuple[Callable[[dict[str, Any], str], HookEvent], Callable[[str, dict[str, Any]], str]]

HOOK_ADAPTERS: dict[str, Adapter] = {
    "hermes": (_normalize_hermes, _serialize_hermes),
    "claude": (_normalize_claude, _serialize_claude),
    "copilot": (_normalize_copilot, _serialize_copilot),
    "cline": (_normalize_cline, _serialize_cline),
    "antigravity": (_normalize_antigravity, _serialize_antigravity),
    "opencode": (_normalize_opencode, _serialize_opencode),
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
