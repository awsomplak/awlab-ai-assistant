"""
Workspace resolution — simplified for parameter-driven architecture.

Auto-detection is REMOVED. The AI Agent must explicitly pass `workspace_path`
to all MCP tools that operate on files. This module retains only:
    - Project root path validation
    - DB_PATH env var resolution (for agent-recall database directory)
    - Helper utilities for path checking

This approach:
    - Eliminates multi-instance MCP server ambiguity
    - Gives the AI Agent full control over which project is active
    - No psutil, no process tree, no __file__ based resolution
"""

import os
import re
from pathlib import Path
from ..config import settings


def resolve_db_path(workspace_path: str | Path | None = None, project_id: str | None = None) -> str:
    """Resolve the agent-recall database path.

    Resolution order:
        1. ``DB_PATH`` env var — overrides everything. The database file
        ``memory.db`` is created inside the specified directory.
        
        2. ``project_id`` parameter — project-isolated DB at
        ``<workspace_path>/.ai/memory-bank/memory/memory_{sanitized}.db``.
        
        3. Project-id file exists — same as #2, using the project-id value
        read from ``<workspace_path>/.ai/project-id`` as sanitized.
        
        4. Fallback — ``~/.awlab-id/agent-memory/memory/memory.db``
        (user-wide directory, shared across all projects when no project-id
        isolation is configured).

    Args:
        workspace_path (str | Path | None): Absolute path to the project workspace root.

        project_id (str | None): Optional explicit project identifier. When provided, uses
        a dedicated per-project database file with no scope chain
        (backward-compatible with ``agent_recall_bridge``).
        When None, uses scope isolation within a shared database.

    Returns:
        str: Absolute path to the database home directory.
    """
    # 1. DB_PATH env var takes highest priority
    if settings.db_path:
        base = Path(os.path.abspath(settings.db_path))
        base.mkdir(parents=True, exist_ok=True)
        return str(base / "memory.db")

    # 2. Explicit project_id → project-isolated path
    if project_id is not None:
        sanitized = _sanitize_project_id(project_id=project_id)
        base = settings.get_memory_bank_dir(workspace_path=workspace_path or "") / "memory"
        base.mkdir(parents=True, exist_ok=True)
        return str(base / f"memory_{sanitized}.db")

    # 3. No explicit project_id, but .ai/project-id file exists → use its value
    pid = settings.get_project_id(workspace_path=workspace_path or "")
    if pid:
        sanitized = _sanitize_project_id(pid)
        base = settings.get_memory_bank_dir(workspace_path=workspace_path or "") / "memory"
        base.mkdir(parents=True, exist_ok=True)
        return str(base / f"memory_{sanitized}.db")

    # 4. No project isolation at all → user-wide shared directory
    base = settings.user_home / ".awlab-id" / "agent-memory" / "memory"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "memory.db")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sanitize_project_id(project_id: str) -> str:
    """
    Sanitize a project ID for use as a database filename.

    Rules:
      - Lowercase
      - Replace any non-alphanumeric character (except hyphen) with underscore
      - Collapse multiple underscores into one
      - Strip leading/trailing underscores
    """
    sanitized = re.sub(r"[^a-z0-9\-]", "_", project_id.lower())
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("_")
    return sanitized or "default"