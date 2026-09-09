from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .exclusions import _GLOBAL_EXCLUSIONS, _gitignore_exclusions, _gitignored


def _identifier_scan(
    workspace_path: str | Path,
    root: Path,
    term: str,
    limit: int = 10,
    max_bytes: int = 1_000_000,
) -> list[dict[str, Any]]:
    """Text-scan source files for an identifier the graph did not index.

    graphify only indexes file/function/class/component-level labels — computed,
    ref, prop, and local variables are NOT graph nodes. This fallback greps the
    (noise + project-gitignore-excluded) source tree for the term as a whole-word
    identifier and returns file-level hits so ``graph_query`` never returns a
    dead end for a real identifier (e.g. a Vue ``ref``/``computed``).
    Case-sensitive first, case-insensitive as a fallback. Never raises.
    """
    if not term or len(term) < 2:
        return []
    pattern = re.compile(r"\b" + re.escape(term) + r"\b")
    pattern_ic = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    exclusions = _gitignore_exclusions(root)
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        parent = "" if rel_dir == "." else rel_dir
        active_ex = exclusions if exclusions is not None else _GLOBAL_EXCLUSIONS
        dirnames[:] = [d for d in dirnames if not active_ex.excludes_dir(parent, d) and not d.endswith(".egg-info")]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(dirpath) / name
            if active_ex.excludes_file(parent, name):
                continue
            if exclusions is not None and _gitignored(root, path, exclusions):
                continue
            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
                if path.stat().st_size > max_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = pattern.search(text)
            if m is None:
                m = pattern_ic.search(text)
            if m is None:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            hits.append(
                {
                    "id": f"id:{rel}",
                    "label": m.group(0),
                    "type": "identifier",
                    "source_file": rel,
                }
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    return hits
