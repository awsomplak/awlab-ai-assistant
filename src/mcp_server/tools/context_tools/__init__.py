"""
context_tools package — Extracted from the monolithic context_tools.py.

Submodules:
  _cache.py   — Shared JSON cache helpers (load, save)
  scanner.py  — Framework detection, scan_project, get_project_fingerprint
  context.py  — Context snapshot, memory search, store context, get fragment
  suggest.py  — suggest_relevant_files (intelligent file suggestions)
"""

# ── Cache (shared JSON helpers) ─────────────────────────────────────────────
from ...helpers.registry_utils import parse_registry as _parse_registry
from ._cache import load_cache as _load_cache
from ._cache import save_cache as _save_cache

# ── Registry Parser ──────────────────────────────────────────────────────────
from ._registry_parser import get_current_phase_from_tasks

# ── Context (snapshot, memory, fragments) ───────────────────────────────────
from .context import get_context_snapshot
from .scanner import (
    detect_framework as _detect_framework,
)

# compute_tasks_summary is available directly from helpers.file_utils
from .scanner import (
    get_cache_path,
    scan_project,
)
from .suggest import (
    suggest_relevant_files,
)

__all__ = [
    "get_context_snapshot",
    "get_cache_path",
    "get_current_phase_from_tasks",
    "_parse_registry",
    "_detect_framework",
    "_load_cache",
    "_save_cache",
    "scan_project",
    "suggest_relevant_files",
]
