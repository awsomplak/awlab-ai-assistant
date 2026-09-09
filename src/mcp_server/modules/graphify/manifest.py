from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    _BACKGROUND_ERRORS,
    _BACKGROUND_PROGRESS,
    _BUILD_LOCKS_GUARD,
    _codegraph_dir,
)


def _manifest_update(out_dir: Path, **fields: Any) -> None:
    """Merge metadata fields into .build_state.json without clobbering the rest.

    Atomic (tmp file + os.replace) so a torn write can never corrupt the
    manifest. Best-effort: no-ops when the manifest is missing/unreadable.
    """
    state_path = out_dir / ".build_state.json"
    if not state_path.is_file():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    state.update(fields)
    tmp = state_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _bg_error(key: str, message: str) -> None:
    """Record a background worker error for a workspace (thread-safe)."""
    with _BUILD_LOCKS_GUARD:
        _BACKGROUND_ERRORS[key] = message
        # Persist so a restart can still surface why the worker died.
        _manifest_update(_codegraph_dir(Path(key)), rebuilding_error=message)


def _mark_progress(key: str) -> None:
    """Stamp the latest per-chunk progress time for a workspace (thread-safe)."""
    with _BUILD_LOCKS_GUARD:
        _BACKGROUND_PROGRESS[key] = time.monotonic()
        _manifest_update(
            _codegraph_dir(Path(key)),
            rebuilding_last_progress_at=datetime.now(timezone.utc).isoformat(),
        )


def _load_manifest(out_dir: Path) -> dict[str, Any] | None:
    """Load the existing .build_state.json manifest, or None if absent/corrupt."""
    state_path = out_dir / ".build_state.json"
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
