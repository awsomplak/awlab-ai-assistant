"""
Central configuration for the awlab-id MCP server.

Provides a singleton ``settings`` object with lazy-loaded paths relative
to a resolved project root (workspace_path).  All ``.ai/`` directory paths
are derived from this single resolution point so that callers never need to
construct ``.ai`` paths manually.

Environment detection:
  - ``AWLAB_ENV=production`` or running as PyInstaller exe → production mode
  - Otherwise → development mode (project-relative paths)

Production config home:  ``~/.awlab-id/agent-memory/``
  - ``.env`` loaded from there (overrides project .env)
  - ``config.json`` loaded from there (overrides project config.json)
  - Logs stored in ``<config_home>/logs/``
"""

import json
import os
import sys
from functools import cached_property
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════
#  Environment detection
# ══════════════════════════════════════════════════════════════════════════

_MODE: str | None = None  # lazy — set on first access


def _is_production() -> bool:
    """Detect if we're running in production (standalone exe) or development."""
    global _MODE
    if _MODE is not None:
        return _MODE == "production"

    # 1. Explicit env var override
    env_mode = os.environ.get("AWLAB_ENV", "").strip().lower()
    if env_mode in ("production", "prod"):
        _MODE = "production"
        return True
    if env_mode in ("development", "dev"):
        _MODE = "development"
        return False

    # 2. PyInstaller frozen exe → production
    if getattr(sys, "frozen", False):
        _MODE = "production"
        return True

    # 3. Default: development
    _MODE = "development"
    return False


def _load_env_file(path: Path) -> None:
    """Load a .env file into os.environ (if it exists)."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'").strip()
            if key and key not in os.environ:
                os.environ[key] = value


def _load_config_file(path: Path) -> dict:
    """Load a JSON config file (if it exists). Returns {} otherwise."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ── Singleton ───────────────────────────────────────────────────────────────


class _Settings:
    """Lazy-loaded settings singleton with production/development awareness."""

    def __init__(self) -> None:
        """Initialize settings with empty state. Call bootstrap() before use."""
        self._user_home: Path | None = None
        self._config: dict = {}
        self._initialized = False

    # ── Bootstrap (call once at server startup) ─────────────────────────────

    def bootstrap(self) -> None:
        """Load .env + config.json from appropriate locations.
        Called once from lifecycle.main() before any tool runs.
        """
        if self._initialized:
            return
        self._initialized = True

        # 1. Load project-level .env (development) or config-home .env (production)
        if self.is_production:
            _load_env_file(self.config_home / ".env")
        else:
            _load_env_file(Path.cwd() / ".env")

        # 2. Load config file
        if self.is_production:
            self._config = _load_config_file(self.config_home / "config.json")
        else:
            self._config = _load_config_file(Path.cwd() / "config.json")

    # ── Mode ────────────────────────────────────────────────────────────────

    @cached_property
    def is_production(self) -> bool:
        """True when running as standalone PyInstaller exe or AWLAB_ENV=production."""
        return _is_production()

    # ── Paths ───────────────────────────────────────────────────────────────

    @cached_property
    def config_home(self) -> Path:
        """Production config directory: ``~/.awlab-id/agent-memory/``."""
        path = self.user_home / ".awlab-id" / "agent-memory"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @cached_property
    def user_home(self) -> Path:
        """The current user's home directory."""
        if self._user_home is not None:
            return self._user_home
        return Path.home()

    @cached_property
    def log_dir(self) -> Path:
        """Log directory: production → ``~/.awlab-id/agent-memory/logs/``, development → ``./logs/``."""
        if self.is_production:
            path = self.config_home / "logs"
        else:
            path = Path.cwd() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_workspace(self, workspace_path: str | Path = "") -> Path:
        """Resolve the project root, falling back to CWD."""
        if workspace_path:
            if isinstance(workspace_path, Path):
                return workspace_path.resolve()
            try:
                return Path(workspace_path).resolve()
            except TypeError:
                raise TypeError(f"Expected str or Path, got {type(workspace_path).__name__}")
        return Path.cwd().resolve()

    # ─── Config values (from env vars, config file, or defaults) ────────────

    def _get(self, key: str, default: str = "") -> str:
        """Resolve a config value: env var → config file → default."""
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val
        cfg_val = self._config.get(key)
        if cfg_val is not None:
            return str(cfg_val)
        return default

    @cached_property
    def db_path(self) -> str | None:
        """Optional ``DB_PATH`` environment variable override."""
        return os.environ.get("DB_PATH") or self._config.get("DB_PATH") or None

    @cached_property
    def enable_log(self) -> bool:
        """Whether file logging is enabled (default True)."""
        val = self._get("LOG_ENABLED", "true")
        return val.strip().lower() in ("true", "1", "yes")

    @cached_property
    def log_level(self) -> str:
        """Log level string (e.g. "info", "debug"). Default from env or "info"."""
        return self._get("LOG_LEVEL", "INFO").strip().lower()

    @cached_property
    def graph_parallel(self) -> bool:
        """Whether graphify extraction uses the ProcessPoolExecutor (default OFF).

        Sequential extraction is proven faster at realistic project scale (Windows
        process-spawn overhead exceeds the small parallelizable portion), and the
        pool hangs in the frozen onefile exe. Opt in for very large corpora via
        ``GRAPH_PARALLEL=1`` (env var or config.json).
        """
        val = self._get("GRAPH_PARALLEL", "false")
        return val.strip().lower() in ("true", "1", "yes")

    # ── .ai/ directory resolvers ────────────────────────────────────────────

    def get_ai_dir(self, workspace_path: str | Path = "") -> Path:
        """Return the path to the ``.ai`` directory for the given workspace.
        All ``.ai/`` sub-directories (artifacts, memory-bank, etc.) are
        derived from this single method so that callers never need to
        hardcode ``.ai``.
        """
        path = self._resolve_workspace(workspace_path=workspace_path) / ".ai"
        if not self.is_production:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def get_artifacts_dir(self, workspace_path: str | Path = "") -> Path:
        """Return the path to the .ai/artifacts directory."""
        path = self.get_ai_dir(workspace_path=workspace_path) / "artifacts"
        if not self.is_production:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def get_memory_bank_dir(self, workspace_path: str | Path = "") -> Path:
        """Return the path to the .ai/memory-bank directory."""
        path = self.get_ai_dir(workspace_path=workspace_path) / "memory-bank"
        if not self.is_production:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def get_registry_path(self, workspace_path: str | Path = "") -> Path:
        """Return the path to the registry.md file."""
        return self.get_artifacts_dir(workspace_path=workspace_path) / "registry.md"

    def get_plan_dir(self, workspace_path: str | Path = "", plan_uuid: str = "") -> Path:
        """Return the directory for a specific plan by UUID."""
        return self.get_artifacts_dir(workspace_path=workspace_path) / plan_uuid

    def get_plan_path(self, workspace_path: str | Path = "", plan_uuid: str = "") -> Path:
        """Return the path to a specific plan's plan.md file."""
        return self.get_plan_dir(workspace_path=workspace_path, plan_uuid=plan_uuid) / "plan.md"

    def get_plan_tasks_path(self, workspace_path: str | Path = "", plan_uuid: str = "") -> Path:
        """Return the path to a specific plan's tasks.md file."""
        return self.get_plan_dir(workspace_path=workspace_path, plan_uuid=plan_uuid) / "tasks.md"

    def get_project_id_path(self, workspace_path: str | Path = "") -> Path:
        """Return the path of project-id."""
        return self.get_ai_dir(workspace_path=workspace_path) / "project-id"

    def get_project_id(self, workspace_path: str | Path = "") -> str | None:
        """Read the project ID from ``{workspace}/.ai/project-id``.
        Returns the first non-empty line or ``None``.
        """
        path = self.get_project_id_path(workspace_path=workspace_path)
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text.splitlines()[0].strip()
            except OSError:
                pass
        return None

    def memory_yaml_path(self, workspace_path: str | Path = "") -> Path | None:
        """Return the path to ``memory.yaml`` under the project root, or ``None``."""
        candidate = self._resolve_workspace(workspace_path=workspace_path) / "memory.yaml"
        return candidate if candidate.exists() else None


# Global singleton — bootstrap will be called from lifecycle.main()
settings = _Settings()


# Singleton instance
settings = _Settings()
