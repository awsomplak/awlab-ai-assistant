from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Helper aliases used in this module (you might need to adjust or add imports if they rely on other modules)


@dataclass
class _ProjectExclusions:
    """Project-derived exclusion rules (parsed from .gitignore + .graphignore).

    Both files use gitignore syntax and are parsed into ONE additive rule set —
    a file/dir excluded by either is excluded from the graph. Patterns are
    applied with the SAME glob semantics to files AND directories (a glob like
    ``dist-*/`` prunes whole directories, not just matching filenames), so
    `.gitignore` and `.graphignore` behave identically for files and dirs.

    Supplements the ALWAYS-applied ``_NOISE_DIRS`` safety net — it can exclude
    MORE (project junk: dist/, build/, coverage/, ...; graph-only exclusions via
    .graphignore) but can NEVER re-include a ``_NOISE_DIRS`` path. Only positive
    patterns are applied (``!`` negations are ignored — conservative), a bare
    ``*``/``**`` (ignore-all, which needs ``!`` re-inclusions) is skipped, and a
    blank-detection guard keeps exclusions from ever emptying the source set.
    """

    dir_names: set[str] = field(default_factory=set)  # exact basenames (file OR dir, any level)
    dir_paths: set[str] = field(default_factory=set)  # exact relative paths (file OR dir → subtree)
    name_globs: list[str] = field(default_factory=list)  # basename globs (files AND dirs, any level)
    path_globs: list[str] = field(default_factory=list)  # relative-path globs (files AND dirs)
    dir_name_globs: list[str] = field(default_factory=list)  # dir-only basename globs (e.g. dist-*/)
    dir_path_globs: list[str] = field(default_factory=list)  # dir-only relative-path globs (e.g. build/*/)

    def excludes_dir(self, parent_rel: str, name: str) -> bool:
        """True when a directory (name under parent_rel) should be pruned."""
        rel = f"{parent_rel}/{name}" if parent_rel else name
        if name in self.dir_names:
            return True
        if rel in self.dir_paths:
            return True
        if any(fnmatch.fnmatch(name, g) for g in self.name_globs):
            return True
        if any(fnmatch.fnmatch(rel, g) for g in self.path_globs):
            return True
        if any(fnmatch.fnmatch(name, g) for g in self.dir_name_globs):
            return True
        return any(fnmatch.fnmatch(rel, g) for g in self.dir_path_globs)

    def excludes_file(self, rel: str, name: str) -> bool:
        """True when a file (rel path + basename) should be excluded."""
        if name in self.dir_names:
            return True
        if rel in self.dir_paths:
            return True
        if any(fnmatch.fnmatch(name, g) for g in self.name_globs):
            return True
        return any(fnmatch.fnmatch(rel, g) for g in self.path_globs)

def _parse_gitignore(ex: _ProjectExclusions, content: str, base_rel: str) -> None:
    """Parse one exclusion file (.gitignore OR .graphignore) into the shared set.

    ``base_rel`` = the file's directory relative to the scan root, so relative
    path patterns are anchored correctly. Files and directories use the SAME
    gitignore-style glob semantics (``*`` / ``?`` / ``[...]``; ``**`` crosses
    directories). Directory-only patterns (trailing ``/``) prune directories;
    plain patterns match a file OR a directory of that name/path.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue  # skip blanks, comments, and negations (conservative)
        # Bare '*'/'**' = "ignore everything in this directory". Without '!'
        # re-inclusions we cannot honor it, and as a GLOBAL basename glob it
        # would exclude every file (the eka-panel stall root cause). Skip it —
        # the directories it guards are normally also excluded by more specific
        # rules or _NOISE_DIRS.
        if line in ("*", "**"):
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1].strip()
        anchored = line.startswith("/")
        if anchored:
            line = line[1:].strip()
        if not line:
            continue
        is_glob = any(ch in line for ch in "*?[")
        if anchored or "/" in line:
            # Relative-path pattern (anchored at the file's dir).
            rel = f"{base_rel}/{line}" if base_rel else line
            rel = rel.strip("/")
            if dir_only:
                if is_glob:
                    ex.dir_path_globs.append(rel)
                else:
                    ex.dir_paths.add(rel)
            else:
                if is_glob:
                    ex.path_globs.append(rel)
                else:
                    # Exact path → excludes that file OR dir (and, for a dir,
                    # everything beneath it via dir pruning).
                    ex.dir_paths.add(rel)
        else:
            # Basename pattern (any level).
            if dir_only:
                if is_glob:
                    ex.dir_name_globs.append(line)
                else:
                    ex.dir_names.add(line)
            else:
                if is_glob:
                    ex.name_globs.append(line)
                else:
                    # Exact name → excludes a file OR a dir with that name.
                    ex.dir_names.add(line)

def _load_global_exclusions() -> _ProjectExclusions:
    ex = _ProjectExclusions()
    ignores_dir = Path(__file__).resolve().parent.parent.parent / "data" / "ignores"
    if ignores_dir.is_dir():
        for f in ignores_dir.glob("*.ignore"):
            try:
                _parse_gitignore(ex, f.read_text(encoding="utf-8", errors="ignore"), "")
            except OSError:
                continue
    return ex

_GLOBAL_EXCLUSIONS = _load_global_exclusions()

def _is_noise_relpath(rel: str) -> bool:
    """True if a forward-slash relpath lives under a noise directory."""
    parts = rel.split("/")
    for i in range(len(parts) - 1):
        p = parts[i]
        if p.endswith(".egg-info"):
            return True
        parent = "/".join(parts[:i])
        if _GLOBAL_EXCLUSIONS.excludes_dir(parent, p):
            return True
    return False

def _source_manifest(root: Path, exclusions: _ProjectExclusions | None = None) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        parent = "" if rel_dir == "." else rel_dir

        active_ex = exclusions if exclusions is not None else _GLOBAL_EXCLUSIONS
        dirnames[:] = [d for d in dirnames if not active_ex.excludes_dir(parent, d) and not d.endswith(".egg-info")]

        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            if name in (".gitignore", ".graphignore", ".gitattributes", ".gitmodules"):
                continue
            path = Path(dirpath) / name
            if active_ex.excludes_file(parent, name):
                continue
            if exclusions is not None and _gitignored(root, path, exclusions):
                continue
            try:
                st = path.stat()
                rel = f"{parent}/{name}" if parent else name
                manifest[rel] = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                continue
    if not manifest and exclusions is not None:
        return _source_manifest(root, None)
    return manifest

_SCANNED_COUNT_CACHE: dict[str, tuple[float, int]] = {}

_SCANNED_TTL = 10.0

def _scanned_count(root: Path) -> int:
    key = str(Path(root).resolve())
    now = time.monotonic()
    cached = _SCANNED_COUNT_CACHE.get(key)
    if cached is not None and now - cached[0] < _SCANNED_TTL:
        return cached[1]
    n = len(_source_manifest(root, None))
    _SCANNED_COUNT_CACHE[key] = (now, n)
    return n

_EXCLUSION_CACHE: dict[
    str,
    tuple[float, _ProjectExclusions, dict[str, int], dict[str, tuple[int, int]]],
] = {}

_EXCLUSION_TTL = 30.0  # seconds — re-walk after this so exclusion edits are picked up


# Exclusion files parsed at every level, gitignore syntax, ADDITIVE (can only
# exclude MORE — never re-include). ``.graphignore`` lets users exclude files/
# dirs from the CODE GRAPH without touching their project .gitignore.
_EXCLUSION_FILENAMES = (".gitignore", ".graphignore")

def _gitignore_exclusions(scan_root: Path) -> _ProjectExclusions:
    """Load all project exclusion rules (.gitignore + .graphignore) under scan_root.

    Both use gitignore syntax and merge into ONE additive rule set — a file
    excluded by either is excluded from the graph. This keeps graph-only
    exclusions in ``.graphignore`` (committed or not) without affecting git.
    Cached with a TTL; the walk prunes ``_NOISE_DIRS`` so dependency/junk
    subtrees are never traversed just to read them.
    """
    key = str(Path(scan_root).resolve())
    now = time.monotonic()
    cached = _EXCLUSION_CACHE.get(key)
    if cached is not None and now - cached[0] < _EXCLUSION_TTL:
        _, cached_ex, cached_dirs, cached_files = cached
        try:
            dirs_current = {path: Path(path).stat().st_mtime_ns for path in cached_dirs}
            files_current = {path: (Path(path).stat().st_mtime_ns, Path(path).stat().st_size) for path in cached_files}
        except OSError:
            dirs_current = {}
            files_current = {}
        if dirs_current == cached_dirs and files_current == cached_files:
            return cached_ex
    ex = _ProjectExclusions()
    ex.dir_names = set(_GLOBAL_EXCLUSIONS.dir_names)
    ex.dir_paths = set(_GLOBAL_EXCLUSIONS.dir_paths)
    ex.name_globs = list(_GLOBAL_EXCLUSIONS.name_globs)
    ex.path_globs = list(_GLOBAL_EXCLUSIONS.path_globs)
    ex.dir_name_globs = list(_GLOBAL_EXCLUSIONS.dir_name_globs)
    ex.dir_path_globs = list(_GLOBAL_EXCLUSIONS.dir_path_globs)
    root = Path(scan_root)
    visited_dirs: dict[str, int] = {}
    exclusion_files: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        parent = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if not ex.excludes_dir(parent, d) and not d.endswith(".egg-info")]
        try:
            visited_dirs[str(Path(dirpath))] = Path(dirpath).stat().st_mtime_ns
        except OSError:
            continue
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        for gi_name in _EXCLUSION_FILENAMES:
            if gi_name in filenames:
                gi_path = Path(dirpath) / gi_name
                try:
                    gi_stat = gi_path.stat()
                    exclusion_files[str(gi_path)] = (gi_stat.st_mtime_ns, gi_stat.st_size)
                    content = gi_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    content = ""
                _parse_gitignore(ex, content, "" if rel_dir == "." else rel_dir)
    _EXCLUSION_CACHE[key] = (now, ex, visited_dirs, exclusion_files)
    return ex

def _gitignored(scan_root: Path, path: Path, ex: _ProjectExclusions) -> bool:
    """True when a file path is excluded by project exclusion rules
    (.gitignore/.graphignore; dirs included)."""
    try:
        rel = str(path.relative_to(scan_root)).replace("\\", "/")
    except ValueError:
        return False
    parts = rel.split("/")
    name = parts[-1]
    if ex.excludes_file(rel, name):
        return True
    for i in range(1, len(parts)):
        if ex.excludes_dir("/".join(parts[: i - 1]) if i > 1 else "", parts[i - 1]):
            return True
    return False

def _changed_files(prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[str]:
    """Diff two manifests → sorted list of changed/removed relpaths.

    ``prev`` is a full build-state dict (as returned by :func:`_load_manifest`);
    ``cur`` may be a full build-state dict OR the raw ``{relpath: value}`` map
    from :func:`_source_manifest` — both callers are handled here.
    """
    p = (prev or {}).get("source_manifest", {}) if prev else {}
    # ``cur`` may be a raw source-manifest map or a full build-state dict.
    c = cur.get("source_manifest", cur) if isinstance(cur, dict) else {}
    changed = [f for f in c if p.get(f) != c.get(f)]
    removed = [f for f in p if f not in c]
    return sorted(set(changed) | set(removed))

# Export _is_noise_relpath for places that used to use _NOISE_DIRS
