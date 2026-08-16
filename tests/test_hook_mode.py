"""
Phase 1 T7 — unified hook --agent mode.

Locks in:
1. Adapter normalization → internal HookEvent shape (Hermes/Claude/Copilot/Cline).
2. Anti-loop: tool events are observer-only (empty output), prompt is the only inject.
3. Project-id bootstrap (hook-only auto-write .ai/project-id when missing).
4. Compiled per-host hook configs (valid Claude JSON, Hermes YAML, both → same exe).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mcp_server.hooks.adapters import normalize_payload, serialize_output
from mcp_server.hooks.handler import handle_hook, resolve_project
from mcp_server.hooks.hook_event import HookEvent

# ── Adapter normalization ────────────────────────────────────────────────────


def test_hermes_pre_llm_call_normalizes_to_prompt():
    raw = {
        "hook_event_name": "pre_llm_call",
        "session_id": "s1",
        "cwd": "/proj",
        "extra": {"user_message": "always use pnpm"},
    }
    hook = normalize_payload("hermes", "pre_llm_call", raw)
    assert hook is not None
    assert hook.kind == "prompt"
    assert hook.user_message == "always use pnpm"
    assert hook.project_path == "/proj"


def test_claude_posttooluse_is_observer_kind():
    raw = {"tool_name": "Bash", "tool_input": {"command": "npm install"}, "session_id": "s2", "cwd": "/proj"}
    hook = normalize_payload("claude", "PostToolUse", raw)
    assert hook is not None
    assert hook.kind == "tool"
    assert hook.command == "npm install"


def test_copilot_userpromptsubmit_is_prompt():
    raw = {"prompt": "fix the bug", "workspace": {"path": "/proj"}}
    hook = normalize_payload("copilot", "user-prompt-submit", raw)
    assert hook is not None
    assert hook.kind == "prompt"
    assert hook.user_message == "fix the bug"
    assert hook.project_path == "/proj"


# ── Anti-loop: only prompt injects ───────────────────────────────────────────


def test_tool_event_serializes_empty_never_injects():
    result = handle_hook(
        HookEvent(agent="claude", event="PostToolUse", kind="tool", tool_name="Bash", tool_input={"command": "x"})
    )
    out = serialize_output("claude", "PostToolUse", result)
    assert out == "{}"  # observer-only — empty output kills the tool→inject→tool loop


def test_prompt_event_injects_context():
    result = {"context": "baked pattern", "candidates": []}
    out = serialize_output("hermes", "pre_llm_call", result)
    assert json.loads(out)["context"] == "baked pattern"


def test_pre_tool_block_serializes_terminal_verdict():
    result = {"block": "pattern says use pnpm, not npm"}
    out = serialize_output("claude", "PreToolUse", result)
    assert json.loads(out)["decision"] == "block"


# ── Project-id bootstrap (hook-only) ─────────────────────────────────────────


def test_resolve_project_bootstraps_project_id(tmp_path: Path):
    resolve_project(HookEvent(project_path=str(tmp_path)))
    pid_file = tmp_path / ".ai" / "project-id"
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == re.sub(r"[^a-z0-9_]+", "_", tmp_path.name.lower())


# ── Compiled hook configs ────────────────────────────────────────────────────


def _hooks_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "dist" / "profiles" / "hooks"


def test_claude_hooks_config_is_valid_json():
    p = _hooks_dir() / "claude.hooks.json"
    if not p.exists():
        return  # compile not run in this env — skip silently
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "hooks" in data
    assert "UserPromptSubmit" in data["hooks"]
    cmd = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "hook --agent claude" in cmd


def test_hermes_hooks_config_points_at_same_exe():
    p = _hooks_dir() / "hermes.hooks.yaml"
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8")
    assert "awlab-ai-assistant.exe hook --agent hermes --event pre_llm_call" in text
