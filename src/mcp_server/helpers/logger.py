"""
Professional logging with daily rotation, tool-level tracing, and structured output.

Log format:
    [2026-06-29 14:30:01.123]  INFO  [tool_name] Message
    [2026-06-29 14:30:01.456] ERROR  [tool_name] Message
    │                                       │
    └─ Timestamp (ms precision)             └─ Stack trace on ERROR level

Log files:  {log_dir}/{yyyy-mm-dd}.log
Log dir resolution:
    1. Explicit ``log_dir`` passed to ``Logger()``
    2. ``settings.log_dir`` (production: ``~/.awlab-id/agent-memory/logs/``,
       development: ``<cwd>/logs/``)
    3. System temp directory (last resort)

Policies:
    - ERROR level always also writes to stderr (visible in VS Code MCP logs)
    - Old logs older than 30 days are auto-pruned on init
    - Tool-scoped logging via ``log.tool(name)`` context helper
"""

import os
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════
#  Request ID — per-action_call correlation
# ══════════════════════════════════════════════════════════════════════════
#
# Async-safe via contextvars: each `asyncio.Task` (and the thread it spawns via
# `to_thread`) gets its own copy of the context, so concurrent `action_call`
# invocations cannot bleed their `request_id` into each other's log lines. The
# dispatcher sets this at the top of `_action_call` and resets it in `finally`,
# so a long-running handler can still `await` deep into the stack and every
# `[req=abcd1234]` tag stays bound to the right invocation.
#
# Tag format: `[req=xxxxxxxx]` (8 hex chars, first 4 bytes of uuid4) — short
# enough to fit in a single log line, unique enough that collisions within
# a session are negligible.

_request_id_var: ContextVar[str] = ContextVar("awlab_request_id", default="-")


def set_request_id(rid: str) -> None:
    """Stamp the current request_id for this async task / thread context."""
    _request_id_var.set(rid)


def get_request_id() -> str:
    """Return the current request_id (or "-" outside any action_call)."""
    return _request_id_var.get()


# ══════════════════════════════════════════════════════════════════════════
#  Tool context helper
# ══════════════════════════════════════════════════════════════════════════


class _ToolLog:
    """Lightweight logger scoped to a specific tool call."""

    def __init__(self, parent: "Logger", tool_name: str):
        self._parent = parent
        self._tool = tool_name

    def info(self, message: str) -> None:
        self._parent._write("INFO", message, tool=self._tool)

    def debug(self, message: str) -> None:
        if self._parent._debug_mode:
            self._parent._write("DEBUG", message, tool=self._tool)

    def warning(self, message: str) -> None:
        self._parent._write("WARNING", message, tool=self._tool)

    def error(self, message: str, exc_info: bool = True) -> None:
        self._parent._write("ERROR", message, tool=self._tool, exc_info=exc_info)


# ══════════════════════════════════════════════════════════════════════════
#  Logger
# ══════════════════════════════════════════════════════════════════════════


class Logger:
    """Daily-rotating logger with tool-level tracing, auto-prune, and stderr fallback."""

    def __init__(self, log_dir: str | Path | None = None):
        # ── Config from settings (fallback chain) ──────────────────────────
        self._enabled: bool = self._env_bool("LOG_ENABLED", True)
        level_raw = os.environ.get("LOG_LEVEL", "INFO").strip().lower()
        self._debug_mode: bool = level_raw == "debug"

        # ── Resolve log directory ─────────────────────────────────────────
        if log_dir is None:
            log_dir = self._resolve_log_dir()
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        # ── Auto-prune logs older than 30 days ────────────────────────────
        self._prune_old_logs()

    # ── Public API ─────────────────────────────────────────────────────────

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str, exc_info: bool = True) -> None:
        self._write("ERROR", message, exc_info=exc_info)

    def debug(self, message: str) -> None:
        if self._debug_mode:
            self._write("DEBUG", message)

    def tool(self, name: str) -> _ToolLog:
        """Return a tool-scoped logger for structured tracing."""
        return _ToolLog(self, name)

    # ── Internal ──────────────────────────────────────────────────────────

    def _write(self, level: str, message: str, tool: str = "", exc_info: bool = False) -> None:
        if not self._enabled:
            return

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        date_stamp = now.strftime("%Y-%m-%d")

        # Request ID tag (async-safe via contextvars; "-" when not in an action_call)
        rid = _request_id_var.get()
        rid_tag = f"[req={rid}]" if rid and rid != "-" else ""
        # Final tag order: [tool_name][req=id] — tool first, request-id second,
        # so log scrapers can grep either independently.
        tool_tag = f"[{tool}]" if tool else ""
        tag = f"  {tool_tag}{rid_tag}" if (tool_tag or rid_tag) else ""
        line = f"[{timestamp}] {level:5s}{tag} {message}"

        if exc_info:
            tb = traceback.format_exc()
            if tb and tb.strip() not in ("NoneType: None", ""):
                line += "\n" + tb

        # Always write ERROR to stderr so VS Code MCP logs capture it
        if level == "ERROR":
            print(line, file=sys.stderr, flush=True)

        # Write to daily log file
        try:
            log_path = self._log_dir / f"{date_stamp}.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            print(line, file=sys.stderr, flush=True)

    def _resolve_log_dir(self) -> Path:
        """Resolve the best available log directory."""
        # 1: settings.log_dir (production: ~/.awlab-id/agent-memory/logs/)
        try:
            from ..config import settings

            return settings.log_dir
        except Exception:
            pass

        # 2: System temp (last resort)
        import tempfile

        return Path(tempfile.gettempdir()) / "awlab-id" / "logs"

    def _prune_old_logs(self, max_days: int = 30) -> None:
        """Remove log files older than ``max_days``."""
        if not self._log_dir.exists():
            return
        cutoff = datetime.now().timestamp() - (max_days * 86400)
        for f in self._log_dir.iterdir():
            if f.suffix == ".log" and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        val = os.environ.get(key, "").strip().lower()
        if not val:
            return default
        return val in ("true", "1", "yes")


# Global singleton
logger = Logger()
