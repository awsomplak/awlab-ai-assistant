"""
Shared cache helpers for context_tools/ submodules.

Provides JSON-based file caching with TTL expiry.
"""

import json
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import (
    read_utf8,
    write_utf8,
    resp_json,
)


def load_cache(
    workspace_path: str | Path,
    cache_path: str,
) -> dict[str, Any] | None:
    """Load a JSON cache file, returning None on failure.

    Args:
        workspace_path: Project root path. If empty, falls back to CWD.
        cache_path: Relative path under ``.ai/`` (e.g. ``.context_store.json``).

    Returns:
        Parsed dict or None on failure.
    """
    try:
        root = settings.get_ai_dir(workspace_path=workspace_path)
        cache_file = root / cache_path
        if cache_file.is_file():
            content = read_utf8(path=cache_file)
            if content is not None:
                data = json.loads(content)
                return data if isinstance(data, dict) else None
            return None
    except (json.JSONDecodeError, OSError):
        pass
    return None


def save_cache(
    workspace_path: str | Path,
    cache_path: str,
    data: dict[str, Any],
) -> bool:
    """Save a JSON cache file.

    Args:
        workspace_path: Project root path. If empty, falls back to CWD.
        cache_path: Relative path under ``.ai/``.
        data: Data to serialise.

    Returns:
        True on success.
    """
    try:
        root = settings.get_ai_dir(workspace_path=workspace_path)
        cache_file = root / cache_path
        content = resp_json(**data)
        cached = write_utf8(path=cache_file, content=content)
        # cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return cached
    except OSError:
        return False
