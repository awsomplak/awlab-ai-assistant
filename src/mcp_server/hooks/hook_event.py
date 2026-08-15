"""
Hook event model — the normalized internal contract the bake core consumes.

Every host payload is normalized into a :class:`HookEvent`; the bake core reads ONLY this
shape and never sees host-specific fields. ``kind`` is derived from the event name via the
host adapter (prompt / tool / pre_tool / stop / session / subagent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookEvent:
    """Normalized hook event shared by all hosts."""

    agent: str = ""
    event: str = ""
    kind: str = ""  # prompt | tool | pre_tool | stop | session | subagent
    project_path: str = ""
    project_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    user_message: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    assistant_response: str = ""
    subagent: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def command(self) -> str:
        """Extract the shell command from tool_input, if any."""
        if not self.tool_input:
            return ""
        return str(self.tool_input.get("command") or self.tool_input.get("tool_input") or "")


# Event kinds (anti-loop map: which kind may inject vs observer-only).
EVENT_KINDS = ("prompt", "tool", "pre_tool", "stop", "session", "subagent")
