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
from datetime import datetime
from pathlib import Path

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

    # ── Log File Management ───────────────────────────────────────────────

    def get_log_path(self, date: datetime | None = None) -> Path:
        """Return the log file path for a given date (default: today)."""
        if date is None:
            date = datetime.now()
        return self._log_dir / f"{date.strftime('%Y-%m-%d')}.log"

    def tail(self, n: int = 50) -> list[str]:
        """Return the last ``n`` lines of today's log."""
        path = self.get_log_path()
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-n:]]

    # ── Internal ──────────────────────────────────────────────────────────

    def _write(self, level: str, message: str, tool: str = "", exc_info: bool = False) -> None:
        if not self._enabled:
            return

        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        date_stamp = now.strftime("%Y-%m-%d")

        tag = f"  [{tool}]" if tool else ""
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
