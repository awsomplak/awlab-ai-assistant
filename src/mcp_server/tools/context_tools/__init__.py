"""
context_tools package — Extracted from the monolithic context_tools.py.

Submodules:
  _cache.py   — Shared JSON cache helpers (load, save)
  scanner.py  — Framework detection, scan_project, get_project_fingerprint
  context.py  — Context snapshot, memory search, store context, get fragment
  suggest.py  — suggest_relevant_files (intelligent file suggestions)
"""

# ── Cache (shared JSON helpers) ─────────────────────────────────────────────
from ._cache import load_cache as _load_cache, save_cache as _save_cache

# ── Context (snapshot, memory, fragments) ───────────────────────────────────
from .context import (
    get_context_snapshot,
    store_context,
    get_context_fragment,
    get_context_path
)

# ── Registry Parser ──────────────────────────────────────────────────────────
from ._registry_parser import get_current_phase_from_tasks
from ...helpers.registry_utils import parse_registry as _parse_registry

# compute_tasks_summary is available directly from helpers.file_utils
from .scanner import (
    scan_project,
    detect_framework as _detect_framework,
    get_cache_path,
)
from .suggest import (
    suggest_relevant_files,
)

__all__ = [
    "get_context_snapshot",
    "store_context",
    "get_context_fragment",
    "get_context_path",
    "get_cache_path",
    "get_current_phase_from_tasks",
    "_parse_registry",
    "_detect_framework",
    "_load_cache",
    "_save_cache",
    "scan_project",
    "suggest_relevant_files",
]
