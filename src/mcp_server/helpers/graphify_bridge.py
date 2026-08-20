"""
Graphify bridge — build and query a per-project code knowledge graph.

Library-import integration with the ``graphifyy`` package (no CLI subprocess, no
vendoring), contained per project under ``<root>/.ai/codegraph/``:

- ``build_graph``    — AST-only structural build (detect → extract → build → cluster
                       → export graph.json + graph.html) + writes ``.build_state.json``
- ``graph_status``   — freshness check (source mtimes vs manifest)
- ``ensure_fresh``   — idempotent: rebuild only when missing or stale

Used by the ``graph_*`` actions in the REGISTRY (``graph_build``, ``graph_status``,
``graph_query``, ``graph_path``, ``graph_explain``) and the ``graph_fresh`` precondition.
No LLM pass — structural graph only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from .agent_recall import family_member_id, family_members, seed_member_project_id, sync_family_project_ids
from .response import fail_obj, ok_obj


# graphify's ``extract(parallel=True)`` uses a ProcessPoolExecutor. Default OFF:
# sequential is proven faster at realistic project scale (Windows spawn overhead
# exceeds the small parallelizable portion), and the pool hangs in the frozen
# onefile exe. Opt in for very large corpora via the central config
# ``settings.graph_parallel`` (env ``GRAPH_PARALLEL=1`` or config.json).
def _use_parallel() -> bool:
    """Whether graphify extraction should use the ProcessPoolExecutor.

    Single source of truth: ``settings.graph_parallel`` (see ``config.py``) — so
    the toggle lives in one place alongside log-level and other env settings.
    """
    return settings.graph_parallel


# A stale graph whose rebuild would be heavy (first build or many changed files)
# runs in a background thread so the MCP request returns immediately; the current
# graph.json stays readable thanks to graphify's atomic writes. Small incremental
# rebuilds stay synchronous so a read right after an edit returns accurate data.
_BACKGROUND_THRESHOLD = 20  # changed files at/above which rebuild runs in background

# Per-project serialization: one build at a time per workspace (sync or
# background); different projects use separate locks and build concurrently.
_BUILD_LOCKS: dict[str, threading.RLock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()

# ── Large-graph HTML viz (enhanced filter + drill-down layer) ──────────────
# The injected client-side layer (path filter, min-degree slider, 2-hop focus,
# community drill-down) makes graph.html usable on projects with thousands of
# nodes. vis.js forceAtlas2 physics freezes the browser above this many visible
# nodes, so the layer disables physics until the user narrows the view.
_VIZ_PHYSICS_LIMIT = 2000
# Per-community member cap embedded into the aggregated view's drill-down
# payload (bounds the HTML size on very large graphs; the UI notes the overflow).
_VIZ_DRILLDOWN_CAP = 1500
# Marker used to make the injected layer idempotent across rebuilds.
_VIZ_MARKER = "awlab-large-viz"
_BACKGROUND_THREADS: dict[str, threading.Thread] = {}
_BACKGROUND_ERRORS: dict[str, str] = {}
# Last per-chunk progress timestamp (time.monotonic) per workspace — the stall
# watchdog uses it to detect a worker that is alive but never finishes a chunk.
_BACKGROUND_PROGRESS: dict[str, float] = {}

# A background worker that has not finished a chunk within this window is treated
# as stalled: its failure is surfaced via background_error and it no longer counts
# as "in flight", so subsequent builds are not blocked forever by a zombie thread.
_BACKGROUND_STALL_SECONDS = 600.0
# Bounded wait on the per-project build lock — a genuinely stuck build can never
# block a caller forever; it fails with a clear error instead.
_BUILD_LOCK_TIMEOUT = 600.0


def _build_lock(root: Path) -> threading.RLock:
    """Return the per-project reentrant lock (one per resolved workspace root)."""
    key = str(Path(root).resolve())
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault(key, threading.RLock())


# Worker-lifecycle fields persisted into .build_state.json so a server restart
# can detect (and auto-clear) a stale `rebuilding` flag that has no live worker.
_REBUILD_STATE_KEYS = (
    "rebuilding",
    "rebuilding_started_at",
    "rebuilding_last_progress_at",
    "rebuilding_error",
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


def _background_rebuild(workspace_path: str | Path, root: Path, chunk_size: int | None = None) -> bool:
    """Start a background rebuild for the project (single-flight per workspace).

    Returns True if a new background thread was started, False if one is already
    in flight for this project. The thread runs ``build_graph`` (which takes the
    per-project lock) and never raises — a failed background build must not take
    the server down; the next read will simply retry.

    With ``chunk_size`` set, the worker runs Laravel-queue style: it keeps
    processing bounded chunks until ``remaining_files`` reaches 0, so a large
    catch-up finishes smoothly in the background instead of one long spike. A
    stall watchdog surfaces a real ``background_error`` if the worker is alive
    but never finishes a chunk, so a stuck build is never silently null.
    """
    key = str(Path(root).resolve())
    with _BUILD_LOCKS_GUARD:
        existing = _BACKGROUND_THREADS.get(key)
        if existing is not None and existing.is_alive():
            return False
        _BACKGROUND_ERRORS.pop(key, None)
        _BACKGROUND_PROGRESS.pop(key, None)
        # Persist the lifecycle marker so a restart can tell a build was running
        # (and clear it once no live worker remains).
        _started_at = datetime.now(timezone.utc).isoformat()
        _manifest_update(
            _codegraph_dir(Path(key)),
            rebuilding=True,
            rebuilding_started_at=_started_at,
            rebuilding_last_progress_at=_started_at,
            rebuilding_error=None,
        )

        def _run() -> None:
            try:
                if chunk_size:
                    # Queue worker: each iteration processes <= chunk_size files
                    # and advances the manifest; loop until the graph is complete.
                    # Safety: break if remaining_files stops decreasing, so a
                    # manifest/advance bug can never spin the worker forever.
                    last_remaining = None
                    while True:
                        _mark_progress(key)
                        res = build_graph(workspace_path, root, chunk_size=chunk_size)
                        _mark_progress(key)
                        if not res.get("success"):
                            _bg_error(key, str(res.get("error", "chunk build failed")))
                            break
                        remaining = res.get("remaining_files")
                        # 0 → complete; None → the build did not report a chunk
                        # (not chunked / unknown) — stop rather than spin.
                        if remaining == 0 or remaining is None:
                            break
                        if remaining == last_remaining:
                            _bg_error(
                                key,
                                f"chunk worker stopped because remaining_files did not decrease (still {remaining})",
                            )
                            break
                        last_remaining = remaining
                else:
                    _mark_progress(key)
                    res = build_graph(workspace_path, root)
                    _mark_progress(key)
                    if not res.get("success"):
                        _bg_error(key, str(res.get("error", "background build failed")))
            except Exception as exc:  # noqa: BLE001 — background, never crash the server
                _bg_error(key, f"background worker exception: {exc}")
            finally:
                with _BUILD_LOCKS_GUARD:
                    _BACKGROUND_THREADS.pop(key, None)
                    # Worker finished (success/error/stall) — clear the live
                    # marker; keep rebuilding_error so status can report it.
                    _manifest_update(
                        _codegraph_dir(Path(key)),
                        rebuilding=False,
                        rebuilding_last_progress_at=None,
                    )

        t = threading.Thread(target=_run, name=f"graph-rebuild-{key[-24:]}", daemon=True)
        _BACKGROUND_THREADS[key] = t
        t.start()

        # Stall watchdog: a worker that is alive but never finishes a chunk must
        # surface a real background_error (never silently null) and stop blocking
        # the in-flight guard. Python cannot kill a stuck thread, but clearing
        # the in-flight marker lets callers attempt recovery — they will wait on
        # the bounded build lock and get a clear error if the zombie holds it.
        def _watchdog() -> None:
            while True:
                time.sleep(5)
                with _BUILD_LOCKS_GUARD:
                    current = _BACKGROUND_THREADS.get(key)
                    if current is not t or not t.is_alive():
                        return  # worker finished or replaced
                    last = _BACKGROUND_PROGRESS.get(key)
                    stalled = last is None or (time.monotonic() - last) > _BACKGROUND_STALL_SECONDS
                if stalled and key not in _BACKGROUND_ERRORS:
                    _bg_error(
                        key,
                        "background worker stalled: no chunk completed within "
                        f"{_BACKGROUND_STALL_SECONDS:.0f}s — the build lock may be stuck; "
                        "restart the server if it never drains",
                    )

        threading.Thread(target=_watchdog, name=f"graph-watchdog-{key[-16:]}", daemon=True).start()
        return True


def _rebuild_in_flight(workspace_path: str | Path, root: str | Path | None = None) -> bool:
    """True if a background rebuild is currently running AND making progress.

    A worker that has not finished a chunk within the stall window is treated as
    stuck → not "in flight", so subsequent builds are not blocked forever by a
    zombie thread (they will wait on the bounded build lock instead).
    """
    # The output root identifies the project; ``root`` may only scope the scan.
    key = str(_resolve_root(workspace_path))
    with _BUILD_LOCKS_GUARD:
        t = _BACKGROUND_THREADS.get(key)
        if t is None or not t.is_alive():
            return False
        last = _BACKGROUND_PROGRESS.get(key)
        if last is not None and (time.monotonic() - last) > _BACKGROUND_STALL_SECONDS:
            return False  # alive but stalled → not a healthy in-flight build
        return True


def _graph_freshness(
    workspace_path: str | Path,
    root: str | Path | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Freshness metadata attached to every graph read result.

    This is what lets the AGENT tell whether the data it just read is current and
    whether a background rebuild is in flight — without it, a stale read looks
    identical to a fresh one and the agent silently acts on outdated structure.
    """
    st = graph_status(workspace_path, root, family=family)
    return {
        "graph_fresh": bool(st.get("fresh", False)),
        "graph_exists": bool(st.get("exists", False)),
        "graph_rebuilding": _rebuild_in_flight(workspace_path, root),
        "graph_built_at": st.get("built_at"),
    }


# Directories never indexed as source (mirrors graphify's own noise exclusions).
_NOISE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "vendor",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ai",
    ".temp",
    "logs",
    ".eggs",
    "*.egg-info",
}

# Generated dependency LOCK files — committed for reproducible builds but NOT
# source for the code graph (huge, machine-written, no structure value). Unlike
# .gitignore junk they are typically COMMITTED, so they need an explicit
# always-on exclusion (never relaxed by gitignore).
_LOCK_FILES = {
    # JS / Node
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "deno.lock",
    # Python
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "pdm.lock",
    # PHP
    "composer.lock",
    # Go / Rust / Ruby
    "go.sum",
    "Gopkg.lock",
    "Cargo.lock",
    "Gemfile.lock",
    # .NET / Java
    "packages.lock.json",
    "paket.lock",
    "gradle.lockfile",
}


def _is_lock_file(name: str) -> bool:
    """True for generated dependency lock files (never graph source)."""
    return name in _LOCK_FILES


def _is_noise_relpath(rel: str) -> bool:
    """True if a forward-slash relpath lives under a noise directory.

    Used to filter graphify's ``detect`` output (which does not honour our
    ``_NOISE_DIRS``) so scratch/temp and dependency dirs never reach ``extract``.
    """
    parts = rel.split("/")
    return any(p in _NOISE_DIRS or p.endswith(".egg-info") for p in parts[:-1])


def _source_manifest(root: Path, exclusions: _ProjectExclusions | None = None) -> dict[str, str]:
    """Return {relpath: '<mtime_ns>:<size>'} for all source files under root.

    Uses ``st_mtime_ns`` (nanosecond) so two writes within the same wall-clock
    second are still detected as changed — whole-second ``st_mtime`` misses
    rapid edits, which silently skips incremental rebuilds.

    When ``exclusions`` (project .gitignore rules) is given, matching dirs are
    pruned during the walk and matching files skipped — so freshness is computed
    over the SAME file set the graph actually indexes (gitignored output churn
    never triggers false stale). Defaults to ``_NOISE_DIRS`` only.
    """
    manifest: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not (d.endswith(".egg-info"))]
        if exclusions is not None:
            rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
            parent = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames if not exclusions.excludes_dir(parent, d)]
        for name in filenames:
            # Config/ignore files are metadata, not graph source — they must not
            # count toward scanned/supported files (or drift the freshness diff).
            if name.endswith((".pyc", ".pyo")) or _is_lock_file(name) or name in _EXCLUSION_FILENAMES:
                continue
            path = Path(dirpath) / name
            if exclusions is not None and _gitignored(root, path, exclusions):
                continue
            try:
                st = path.stat()
                rel = str(path.relative_to(root)).replace("\\", "/")
                manifest[rel] = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                continue
    if not manifest and exclusions is not None:
        # BLANK-DETECTION GUARD: exclusion rules would empty the source set —
        # fall back to _NOISE_DIRS-only so the freshness manifest always matches
        # what the graph actually indexes (and a chunked build can never see an
        # empty diff that forces the full-rebuild path). Mirrors the blank-
        # detection guard on the ``files`` list in ``_build_graph_impl``.
        return _source_manifest(root, None)
    return manifest


# Pre-exclusion scanned-file count, memoized briefly so graph_status polling on
# large repos doesn't walk the tree twice per call (exclusion visibility only).
_SCANNED_COUNT_CACHE: dict[str, tuple[float, int]] = {}
_SCANNED_TTL = 10.0


def _scanned_count(root: Path) -> int:
    """Number of source files under root BEFORE project exclusion rules apply.

    Uses the _NOISE_DIRS-only manifest (same walk graph_status already relies on
    for the supported set), memoized for ``_SCANNED_TTL`` seconds. Lets
    ``graph_status`` report how much of the scanned corpus .gitignore /
    .graphignore actually excluded (exclusion effectiveness).
    """
    key = str(Path(root).resolve())
    now = time.monotonic()
    cached = _SCANNED_COUNT_CACHE.get(key)
    if cached is not None and now - cached[0] < _SCANNED_TTL:
        return cached[1]
    n = len(_source_manifest(root, None))
    _SCANNED_COUNT_CACHE[key] = (now, n)
    return n


# ── Project .gitignore-aware exclusion (ADDITIVE to _NOISE_DIRS) ───────────


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
    root = Path(scan_root)
    visited_dirs: dict[str, int] = {}
    exclusion_files: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not d.endswith(".egg-info")]
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


# ── Vite/JS path-alias import augmentation ─────────────────────────────────
#
# graphifyy resolves relative imports and tsconfig/jsconfig ``paths`` aliases,
# but NOT Vite's ``resolve.alias`` (a common Vue/Nuxt/Vite convention where no
# tsconfig exists — e.g. ``'@': './src'`` in vite.config.js). Unresolved alias
# specifiers like ``import { useAuthStore } from '@/stores/auth'`` silently
# produce NO edge, which breaks ``graph_path``/module navigation for .vue SFCs
# and any ``@/``-importing file. This post-build pass (a layer like
# ``_gitignore_exclusions``) teaches the graph about those aliases so the
# missing ``imports_from``/``imports`` edges are added.

_VITE_CONFIG_FILES = ("vite.config.js", "vite.config.mjs", "vite.config.cjs", "vite.config.ts", "vite.config.mts")
_NUXT_CONFIG_FILES = ("nuxt.config.js", "nuxt.config.mjs", "nuxt.config.ts")
_ALIAS_CONFIG_FILES = _VITE_CONFIG_FILES + _NUXT_CONFIG_FILES

# Extension candidates when resolving an extensionless alias specifier
# (``'@/stores/auth'`` → ``stores/auth.js`` / ``stores/auth/index.ts`` ...).
_ALIAS_RESOLVE_EXTS = (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro")


def _resolve_alias_replacement(repl: str, base: Path) -> Path | None:
    """Turn a Vite alias replacement expression into an absolute path.

    Handles the common forms: a plain relative string (``'./src'``), an
    absolute string (``'D:/x/src'`` / ``'/abs'``), and the ubiquitous
    ``fileURLToPath(new URL('./src', import.meta.url))`` wrapper (the URL
    string is extracted and resolved against the config's directory).
    """
    repl = repl.strip()
    url = re.search(r"""new\s+URL\(\s*['"]([^'"]+)['"]""", repl)
    if url:
        repl = url.group(1).strip()
    p = Path(repl)
    if p.is_absolute():
        return p.resolve()
    rel = repl.strip("'\" `,")
    if not rel:
        return None
    return (base / rel).resolve()


def _vite_alias_map(scan_root: Path) -> dict[str, Path]:
    """Discover path aliases (``'@': './src'`` etc.) from Vite/Nuxt config.

    graphifyy reads tsconfig/jsconfig ``paths`` but never Vite's
    ``resolve.alias`` — the config is searched from ``scan_root`` upward
    (≤3 parents) so ``root="src"`` scans still find the project's config.
    Returns {alias key: absolute target dir}, or {} when none is found.
    """
    for base in (scan_root, *scan_root.parents[:3]):
        for name in _ALIAS_CONFIG_FILES:
            cfg = base / name
            if not cfg.is_file():
                continue
            try:
                text = cfg.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            aliases: dict[str, Path] = {}
            # Object form: alias: { '@': './src', '@components': '...' }  (string or URL)
            for m in re.finditer(r"""['"]([@~][^'"]*)['"]\s*:\s*['"]([^'"]+)['"]""", text):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            for m in re.finditer(
                r"""['"]([@~][^'"]*)['"]\s*:\s*fileURLToPath\(\s*new\s+URL\(\s*['"]([^'"]+)['"]""",
                text,
            ):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            # Array form: alias: [ { find: '@', replacement: './src' }, ... ]
            for m in re.finditer(r"""find\s*:\s*['"]([@~][^'"]*)['"]\s*,\s*replacement\s*:\s*['"]([^'"]+)['"]""", text):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            for m in re.finditer(
                r"""find\s*:\s*['"]([@~][^'"]*)['"]\s*,\s*replacement\s*:\s*fileURLToPath\(\s*new\s+URL\(\s*['"]([^'"]+)['"]""",
                text,
            ):
                target = _resolve_alias_replacement(m.group(2), base)
                if target is not None:
                    aliases[m.group(1)] = target
            if aliases:
                return aliases
    return {}


# Import/export statement matcher: named/bare/dynamic forms, single or double
# quotes, multi-line. Group ``spec`` = from-form specifier, ``bare``/``dyn`` =
# side-effect / dynamic-import forms, ``clause`` = the imported-name clause.
# NOTE: kept on ONE line — literal whitespace inside a multi-line pattern is
# matched literally, which silently breaks the regex.
_ALIAS_IMPORT_RE = re.compile(
    r"""\b(?:import|export)\b(?:\s+(?P<clause>\{[^{}]*\}|[^;"']*?)\s+from\s+['"](?P<spec>[^"']+)['"]|\s+['"](?P<bare>[^"']+)['"]|\s*\(\s*['"](?P<dyn>[^"']+)['"]\s*\))""",
    re.M | re.S,
)
_ALIAS_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"]([^"']+)['"]\s*\)""", re.M | re.S)


def _imported_names(clause: str | None) -> list[str]:
    """Extract imported symbol names from an import clause.

    ``{ useAuthStore, type T }`` → [useAuthStore]; ``api`` → [api];
    ``* as ns`` → []; ``''`` → [].
    """
    clause = (clause or "").strip()
    if not clause:
        return []
    if clause.startswith("{"):
        names: list[str] = []
        for part in clause.strip("{} ").split(","):
            part = part.strip()
            if not part or part == "type":
                continue
            if part.startswith("type "):
                part = part[len("type ") :].strip()
            base = part.split(" as ")[0].strip()
            if base and base != "type":
                names.append(base)
        return names
    if clause.startswith("*"):
        return []  # namespace import — no named symbol edge
    base = clause.split(" as ")[0].strip()
    return [base] if base else []


def _alias_import_specifiers(text: str, aliases: dict[str, Path]):
    """Yield (specifier, imported_names, line) for alias imports in ``text``.

    Only specifiers that start with a declared alias prefix (e.g. ``@/...``)
    are yielded — relative and bare-package imports are graphifyy's domain.
    """
    keys = sorted(aliases, key=len, reverse=True)

    def _is_alias(spec: str) -> bool:
        return any(spec == k or spec.startswith(k + "/") for k in keys)

    for m in _ALIAS_IMPORT_RE.finditer(text):
        spec = m.group("spec") or m.group("bare") or m.group("dyn")
        if not spec or not _is_alias(spec):
            continue
        line = text.count("\n", 0, m.start()) + 1
        yield spec, _imported_names(m.group("clause")), line
    for m in _ALIAS_REQUIRE_RE.finditer(text):
        spec = m.group(1)
        if not spec or not _is_alias(spec):
            continue
        line = text.count("\n", 0, m.start()) + 1
        yield spec, [], line


def _resolve_alias_abs(spec: str, aliases: dict[str, Path]) -> Path | None:
    """Resolve an alias specifier to an absolute path, or None if not aliased."""
    for key in sorted(aliases, key=len, reverse=True):
        if spec == key or spec.startswith(key + "/"):
            return aliases[key] / spec[len(key) :].lstrip("/")
    return None


def _match_module_abs(modules: dict[str, str], cand: Path) -> tuple[str, str] | None:
    """Find an existing module node for a resolved alias path.

    Tries the exact path, then extension and ``/index.*`` candidates for
    extensionless specifiers, then a case-insensitive fallback (Windows FS).
    Returns ``(node_id, source_key)`` — ``source_key`` is the exact module
    source path that matched, needed to look up sibling symbol nodes.
    """
    key = str(cand).replace("\\", "/")
    candidates = [key]
    if not Path(cand).suffix:
        for ext in _ALIAS_RESOLVE_EXTS:
            candidates.append(key + ext)
            candidates.append(key + "/index" + ext)
    for c in candidates:
        hit = modules.get(c)
        if hit is not None:
            return hit, c
    lowered = {k.lower(): v for k, v in modules.items()}
    for c in candidates:
        hit = lowered.get(c.lower())
        if hit is not None:
            return hit, c
    return None


def _mk_alias_link(source: str, target: str, relation: str, src: str, line: int) -> dict[str, Any]:
    """A graphify-style link for an alias-resolved import edge."""
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "context": "import",
        "confidence": "EXTRACTED",
        "source_file": src,
        "source_location": f"L{line}",
        "weight": 1.0,
        "alias_resolved": True,
    }


def _compute_alias_edges(
    node_items: list[tuple[str, str, str]],
    scan_root: Path,
    aliases: dict[str, Path],
) -> list[dict[str, Any]]:
    """Compute missing alias-import links from graph nodes + a source re-scan.

    ``node_items`` is an iterable of ``(node_id, source_file, label)``. Module
    nodes (label == source basename) are re-scanned for alias imports; each
    resolvable specifier yields an ``imports_from`` module edge plus ``imports``
    symbol edges for named imports that resolve to existing symbol nodes.
    """
    modules: dict[str, str] = {}  # abs source path (fwd-slash) -> module node id
    symbols: dict[tuple[str, str], str] = {}  # (abs src, name lower) -> symbol node id
    for nid, src, label in node_items:
        if not src:
            continue
        key = str((scan_root / src).resolve()).replace("\\", "/")
        modules.setdefault(key, nid)
        base = (label or "").split("(", 1)[0].strip()
        if base:
            symbols.setdefault((key, base.lower()), nid)

    links: list[dict[str, Any]] = []
    for nid, src, label in node_items:
        if not src or (label or "") != src.rsplit("/", 1)[-1]:
            continue  # only module nodes import other files
        file = scan_root / src
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for spec, names, line in _alias_import_specifiers(text, aliases):
            cand = _resolve_alias_abs(spec, aliases)
            if cand is None:
                continue
            matched = _match_module_abs(modules, cand)
            if matched is None or matched[0] == nid:
                continue
            tgt_id, tgt_key = matched
            links.append(_mk_alias_link(nid, tgt_id, "imports_from", src, line))
            for name in names:
                sym_id = symbols.get((tgt_key, name.lower()))
                if sym_id and sym_id != nid:
                    links.append(_mk_alias_link(nid, sym_id, "imports", src, line))
    return links


def _dedup_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate (source, target, relation) links."""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for e in links:
        key = (str(e.get("source")), str(e.get("target")), str(e.get("relation")))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _augment_alias_import_edges(graph, scan_root: Path) -> int:
    """Add missing alias-import edges to the built networkx graph (in place).

    Returns the number of edges added. Idempotent: existing edges are never
    duplicated, and a project without Vite/Nuxt aliases is a no-op.
    """
    aliases = _vite_alias_map(scan_root)
    if not aliases:
        return 0
    node_items = [
        (nid, (attrs.get("source_file") or ""), (attrs.get("label") or "")) for nid, attrs in graph.nodes(data=True)
    ]
    links = _dedup_links(_compute_alias_edges(node_items, scan_root, aliases))
    added = 0
    for e in links:
        src, tgt = str(e.get("source")), str(e.get("target"))
        if graph.has_edge(src, tgt):
            continue
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        graph.add_edge(src, tgt, **attrs)
        added += 1
    return added


def _augment_alias_import_edges_json(graph_path: Path, scan_root: Path) -> int:
    """Self-heal a persisted graph.json with missing alias-import links.

    Used by the fresh-skip path so graphs built before this pass (or by an
    older version) gain the alias edges on their next no-op read without a
    full rebuild. Returns the number of links added; no-op without aliases.
    """
    aliases = _vite_alias_map(scan_root)
    if not aliases or not graph_path.is_file():
        return 0
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    node_items = [
        (str(n.get("id") or ""), (n.get("source_file") or ""), (n.get("label") or "")) for n in data.get("nodes", [])
    ]
    links = _dedup_links(_compute_alias_edges(node_items, scan_root, aliases))
    if not links:
        return 0
    existing = {(str(e.get("source")), str(e.get("target"))) for e in data.get("links", [])}
    new = [e for e in links if (e["source"], e["target"]) not in existing]
    if not new:
        return 0
    data.setdefault("links", []).extend(new)
    graph_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(new)


def _graphify_imports() -> dict[str, Any] | None:
    """Import graphify modules lazily. Returns None with a clear error if unavailable.

    ``GRAPHIFY_OUT`` (read once at graphify import time) is pinned to a RELATIVE
    ``.ai/codegraph`` so that, combined with the ``cache_root=<root>`` passed to
    ``detect``/``extract`` in :func:`build_graph`, graphify's own cache lands in
    ``<root>/.ai/codegraph/cache/`` instead of a stray ``graphify-out/`` at the
    project root. An explicitly-set env override is respected (setdefault).
    ``GRAPHIFY_NO_BACKUP=1`` disables graphify's dated backup snapshot dirs (a
    semantic/curated-only side-effect — harmless belt-and-suspenders for the
    AST-only path, which never writes a semantic marker).
    """
    # Must be set BEFORE any ``from graphify...`` import below (read-once).
    os.environ.setdefault("GRAPHIFY_OUT", ".ai/codegraph")
    os.environ.setdefault("GRAPHIFY_NO_BACKUP", "1")
    try:
        from graphify.build import build_from_json
        from graphify.cluster import cluster
        from graphify.detect import detect
        from graphify.export import to_html, to_json
        from graphify.extract import extract
    except ImportError:  # pragma: no cover - defensive (graphifyy is a core dep)
        return None
    return {
        "detect": detect,
        "extract": extract,
        "build_from_json": build_from_json,
        "cluster": cluster,
        "to_json": to_json,
        "to_html": to_html,
    }


def _codegraph_dir(root: Path) -> Path:
    return root / ".ai" / "codegraph"


def _resolve_root(workspace_path: str | Path, root: str | Path | None = None) -> Path:
    """Resolve the scan root: absolute as-is, relative joined to workspace_path.

    A relative ``root`` (e.g. ``"src"``) MUST resolve against ``workspace_path``
    — never the server CWD, which is arbitrary (frozen exe / stdio server).
    Without this, ``graph_build root="src"`` scans the wrong directory (or
    fails with "no supported source files detected").
    """
    base = Path(workspace_path).resolve()
    if root is None:
        return base
    r = Path(root)
    return r.resolve() if r.is_absolute() else (base / r).resolve()


def _resolve_graph_root(
    workspace_path: str | Path,
    root: str | Path | None = None,
    family: str | None = None,
) -> Path:
    """Resolve the graph root for an op, honoring a project family.

    Family ops target the family's MERGED graph directory (config home — works
    across drives, no common ancestor required). Non-family ops target the
    WORKSPACE root's ``.ai/codegraph`` — ``root`` only scopes the scan, never
    the output location (so ``root="src"`` cannot drop ``.ai`` inside ``src/``).
    """
    if family:
        return _family_codegraph_dir(family)
    return _resolve_root(workspace_path)


# ── Large-graph HTML viz (enhanced filter + drill-down layer) ──────────────


# Colour palette for the injected member-level views (mirrors graphify's
# community colours closely enough for visual distinction).
_VIZ_PALETTE = [
    "#4E79A7",
    "#F28E2B",
    "#E15759",
    "#76B7B2",
    "#59A14F",
    "#EDC948",
    "#B07AA1",
    "#FF9DA7",
    "#9C755F",
    "#BAB0AC",
    "#86BCB6",
    "#D37295",
]


def _viz_js_safe(obj: Any) -> str:
    """JSON-encode for embedding inside a <script> tag (no </script> breakout)."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _community_labels(graph, communities: dict[int, list[str]], top_k: int = 3, max_len: int = 48) -> dict[int, str]:
    """Non-LLM heuristic community labels (for the HTML legend + drill-down).

    Ranks each community's members by degree and joins the top labels (e.g.
    ``"apiHandler, helper"``), falling back to ``"Community N"``. Mirrors
    graphify's CLI label generation but stays AST-only (no LLM pass). Without
    these, graphify's HTML legend renders empty (just "Select All").
    """
    degree = dict(graph.degree())
    labels: dict[int, str] = {}
    for cid, members in communities.items():
        ranked = sorted(members, key=lambda m: degree.get(m, 0), reverse=True)
        names: list[str] = []
        seen: set[str] = set()
        for mid in ranked:
            data = graph.nodes.get(mid, {}) or {}
            label = str(data.get("label") or mid).strip().strip("()")
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            names.append(label)
            if len(names) >= top_k:
                break
        label = ", ".join(names)
        if len(label) > max_len:
            label = label[: max_len - 1] + "…"
        labels[int(cid)] = label or f"Community {cid}"
    return labels


def _drilldown_data(
    graph,
    communities: dict[int, list[str]],
    cap: int | None = None,
    community_labels: dict[int, str] | None = None,
) -> dict:
    """Build community → member rows for the aggregated view's drill-down.

    Returns ``{str(cid): {"label", "count", "members": [[id, label, source_file], ...]}}``
    — the exact data the injected layer needs to expand a community node into
    its member nodes. Bounded by ``cap`` per community to keep the embedded
    payload sane on huge graphs.
    """
    cap = _VIZ_DRILLDOWN_CAP if cap is None else cap
    labels = community_labels or {}
    out: dict[str, Any] = {}
    for cid, members in communities.items():
        rows: list[list[str]] = []
        for mid in members:
            if len(rows) >= cap:
                break
            data = graph.nodes.get(mid, {}) or {}
            rows.append(
                [
                    str(mid),
                    str(data.get("label") or mid),
                    str(data.get("source_file") or ""),
                ]
            )
        out[str(cid)] = {
            "label": labels.get(int(cid), f"Community {cid}"),
            "count": len(members),
            "members": rows,
        }
    return out


def _full_member_view(
    graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Full node/edge data for the aggregated view's member-level exploration.

    graphify's over-limit ``to_html`` embeds ONLY the community meta-graph in
    ``graph.html`` — the member nodes are not present, so drill-down cannot
    rebuild a member subgraph from the page alone. We embed the FULL member
    node/edge list alongside the aggregated view (same shape as graphify's
    RAW_NODES/RAW_EDGES) so the injected layer can swap views. Payload is the
    size of graph.json — fine for a local file.
    """
    labels = community_labels or {}
    node_community = {nid: cid for cid, members in communities.items() for nid in members}
    degree = dict(graph.degree())
    max_deg = max(degree.values(), default=1) or 1
    nodes: list[dict[str, Any]] = []
    for node_id, data in graph.nodes(data=True):
        data = data or {}
        cid = node_community.get(node_id, 0)
        label = str(data.get("label") or node_id)
        deg = degree.get(node_id, 1)
        nodes.append(
            {
                "id": str(node_id),
                "label": label,
                "color": _VIZ_PALETTE[cid % len(_VIZ_PALETTE)],
                "size": round(10 + 30 * (deg / max_deg), 1),
                "font": {"size": 12 if deg >= max_deg * 0.15 else 0, "color": "#ffffff"},
                "title": label,
                "community": cid,
                "community_name": labels.get(int(cid), f"Community {cid}"),
                "source_file": str(data.get("source_file") or ""),
                "file_type": str(data.get("file_type") or ""),
                "degree": deg,
            }
        )
    edges: list[dict[str, Any]] = []
    for u, v, edata in graph.edges(data=True):
        extracted = edata.get("confidence") == "EXTRACTED"
        edges.append(
            {
                "from": str(u),
                "to": str(v),
                "title": str(edata.get("relation") or ""),
                "dashes": not extracted,
                "width": 2 if extracted else 1,
                "color": {"opacity": 0.7 if extracted else 0.35},
            }
        )
    return nodes, edges


def _viz_injection_fragment(
    drilldown: dict,
    master_nodes: list[dict] | None = None,
    master_edges: list[dict] | None = None,
) -> str:
    """Self-contained <style>+<script> that adds filtering + drill-down to a
    graphify-generated graph.html.

    Reuses the globals graphify's exporter leaves in page scope (RAW_NODES,
    RAW_EDGES, nodesDS, edgesDS, network, LEGEND) — no forking of graphify.
    Keeps the SAME DataSet instances (mutated via clear()/add()) so graphify's
    own showInfo/focusNode/community-toggle handlers keep working.

    ``master_nodes/master_edges`` (aggregated view only) carry the FULL member
    node/edge data so the filter bar + drill-down can rebuild a real member
    subgraph (graphify's over-limit HTML embeds only the community meta-graph).
    When absent, the page's RAW_NODES/RAW_EDGES ARE the master dataset.
    """
    drill_json = _viz_js_safe(drilldown)
    master_nodes_json = _viz_js_safe(master_nodes or [])
    master_edges_json = _viz_js_safe(master_edges or [])
    return f"""<!-- {_VIZ_MARKER}: enhanced filter + drill-down layer (self-contained) -->
<style>
  #viz-controls {{ padding: 8px 12px; border-bottom: 1px solid #2a2a4e; font-size: 12px; display: flex; flex-direction: column; gap: 8px; background: #14142a; }}
  #viz-controls .vhead {{ display: flex; align-items: center; justify-content: space-between; }}
  #viz-controls .vhead b {{ color: #aaa; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }}
  #viz-controls .vbody {{ display: flex; flex-direction: column; gap: 8px; }}
  #viz-controls.collapsed .vbody {{ display: none; }}
  #viz-controls .vrow {{ display: flex; align-items: center; gap: 8px; }}
  #viz-controls input[type=text] {{ width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 6px 8px; border-radius: 5px; font-size: 12px; outline: none; box-sizing: border-box; }}
  #viz-controls input[type=text]:focus {{ border-color: #4E79A7; }}
  #viz-controls input[type=range] {{ flex: 1; accent-color: #4E79A7; }}
  #viz-controls label {{ color: #aaa; display: flex; align-items: center; gap: 5px; white-space: nowrap; }}
  #viz-controls button {{ background: #2a2a4e; color: #e0e0e0; border: 1px solid #3a3a5e; border-radius: 5px; padding: 4px 10px; font-size: 12px; cursor: pointer; }}
  #viz-controls button:hover {{ background: #3a3a5e; }}
  #viz-controls .vstat {{ color: #888; font-size: 11px; }}
  #viz-controls .vwarn {{ color: #d9a13b; font-size: 11px; line-height: 1.5; }}
  #viz-drill {{ border-top: 1px solid #2a2a4e; max-height: 42%; overflow-y: auto; padding: 8px 12px; background: #14142a; display: flex; flex-direction: column; gap: 6px; }}
  #viz-drill h4 {{ font-size: 12px; color: #aaa; margin: 0; text-transform: uppercase; letter-spacing: .05em; }}
  #viz-drill input[type=text] {{ width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 5px 8px; border-radius: 5px; font-size: 12px; outline: none; box-sizing: border-box; }}
  #viz-drill .drow {{ padding: 3px 6px; border-radius: 3px; cursor: pointer; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  #viz-drill .drow:hover {{ background: #2a2a4e; }}
  #viz-drill .dpath {{ color: #666; font-size: 10px; margin-left: 6px; }}
  /* Node Info box: graphify's CSS leaves #info-panel as `flex: 0 1 auto;
     overflow: visible`, so in the fixed-height sidebar it gets squeezed and its
     content spills over the Communities legend below. Fix: never shrink the
     panel below its content (flex 0 0 auto) and cap it so the box scrolls
     internally — long neighbor lists no longer overlap or push the legend,
     which keeps the remaining sidebar height. */
  #info-panel {{ flex: 0 0 auto; max-height: 240px; overflow-y: auto; }}
  /* Draggable splitter between Node Info and Communities: drag to give either
     pane more room; double-click resets to the automatic height. */
  #viz-splitter {{ flex: 0 0 auto; height: 7px; cursor: row-resize; background: #1a1a2e; border-top: 1px solid #2a2a4e; border-bottom: 1px solid #2a2a4e; }}
  #viz-splitter:hover, #viz-splitter.dragging {{ background: #3a3a5e; }}
  body.viz-resizing {{ cursor: row-resize; user-select: none; }}
</style>
<script>
(function () {{
  if (typeof RAW_NODES === 'undefined' || typeof RAW_EDGES === 'undefined' || typeof network === 'undefined') return;
  var DRILLDOWN = {drill_json};
  var PHYSICS_LIMIT = {_VIZ_PHYSICS_LIMIT};
  var FILTER_HOPS = 2;
  var FOCUSED_ID = null;

  // Master dataset = full member data (aggregated view) or the page's RAW
  // (full view). RAW_NODES/RAW_EDGES always hold the INITIAL rendered view
  // (meta-graph for aggregated), so Reset restores it.
  var MASTER_NODES = {master_nodes_json};
  var MASTER_EDGES = {master_edges_json};
  var nodes = MASTER_NODES.length ? MASTER_NODES : RAW_NODES;
  var edges = MASTER_EDGES.length ? MASTER_EDGES : RAW_EDGES;
  var TOTAL_NODES = nodes.length;
  var TOTAL_EDGES = edges.length;

  function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }}

  // Adjacency from the master dataset so focus mode uses the FULL graph (not
  // the current, possibly filtered, view).
  var ADJ = {{}};
  edges.forEach(function (e) {{ var a = String(e.from), b = String(e.to); (ADJ[a] = ADJ[a] || []).push(b); (ADJ[b] = ADJ[b] || []).push(a); }});
  function neighborsOf(id) {{ return ADJ[id] || []; }}

  var wrap = document.getElementById('search-wrap');
  if (!wrap) return;
  var ctl = document.createElement('div');
  ctl.id = 'viz-controls';
  ctl.innerHTML =
    '<div class="vhead"><b>Filters</b><button id="f-toggle" title="Show / hide the filter bar">−</button></div>' +
    '<div class="vbody">' +
      '<div class="vrow"><input id="f-path" type="text" placeholder="Filter by path (e.g. src/components)…" autocomplete="off"></div>' +
      '<div class="vrow"><input id="f-deg" type="range" min="0" max="1" value="0"><label>min degree <b id="f-deg-v">0</b></label></div>' +
      '<div class="vrow"><label><input id="f-focus" type="checkbox"> Focus 2-hop</label><span style="flex:1"></span><button id="f-stab" title="Run the layout on the visible nodes">Stabilize</button></div>' +
      '<div class="vrow"><button id="f-apply" style="flex:1">Apply filters</button><button id="f-reset">Reset</button></div>' +
      '<div class="vstat" id="f-stats"></div>' +
      '<div class="vwarn" id="f-warn" style="display:none"></div>' +
    '</div>';
  wrap.after(ctl);

  // Filter bar show/hide toggle — collapse it to free sidebar space.
  document.getElementById('f-toggle').addEventListener('click', function () {{
    ctl.classList.toggle('collapsed');
    this.textContent = ctl.classList.contains('collapsed') ? '+' : '−';
  }});

  // Draggable splitter between Node Info and Communities.
  var infoPanel = document.getElementById('info-panel');
  var legendWrap = document.getElementById('legend-wrap');
  if (infoPanel && legendWrap) {{
    var splitter = document.createElement('div');
    splitter.id = 'viz-splitter';
    splitter.title = 'Drag to resize · double-click to reset';
    infoPanel.after(splitter);
    var dragging = false, startY = 0, startH = 0;
    splitter.addEventListener('mousedown', function (e) {{
      dragging = true; startY = e.clientY;
      startH = infoPanel.getBoundingClientRect().height;
      splitter.classList.add('dragging');
      document.body.classList.add('viz-resizing');
      if (e.preventDefault) e.preventDefault();
    }});
    window.addEventListener('mousemove', function (e) {{
      if (!dragging) return;
      var top = infoPanel.getBoundingClientRect().top;
      var max = legendWrap.getBoundingClientRect().bottom - top - 60;
      var h = Math.min(Math.max(startH + (e.clientY - startY), 60), Math.max(max, 60));
      infoPanel.style.height = h + 'px';
      infoPanel.style.maxHeight = 'none';
    }});
    window.addEventListener('mouseup', function () {{
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove('dragging');
      document.body.classList.remove('viz-resizing');
    }});
    splitter.addEventListener('dblclick', function () {{
      infoPanel.style.height = '';
      infoPanel.style.maxHeight = '';
    }});
  }}

  var maxDeg = 0;
  nodes.forEach(function (n) {{ if ((n.degree || 0) > maxDeg) maxDeg = n.degree || 0; }});
  document.getElementById('f-deg').max = Math.max(1, maxDeg);

  function nodeObj(n) {{
    return {{ id: n.id, label: n.label, color: n.color, size: n.size, font: n.font, title: n.title, _community: n.community, _community_name: n.community_name, _source_file: n.source_file, _file_type: n.file_type, _degree: n.degree }};
  }}
  function edgeObj(e, i) {{
    return {{ id: i, from: e.from, to: e.to, label: '', title: e.title, dashes: e.dashes, width: e.width, color: e.color, arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }} }};
  }}
  function statsLine(vn, ve) {{
    var nc = (typeof LEGEND !== 'undefined' && LEGEND) ? LEGEND.length : 0;
    document.getElementById('f-stats').textContent = vn.length + ' / ' + TOTAL_NODES + ' nodes · ' + ve.length + ' edges · ' + nc + ' communities';
  }}
  function showWarn(msg) {{ var w = document.getElementById('f-warn'); if (msg) {{ w.textContent = msg; w.style.display = 'block'; }} else {{ w.style.display = 'none'; }} }}

  function computeVisible() {{
    var q = document.getElementById('f-path').value.trim().toLowerCase();
    var minDeg = parseInt(document.getElementById('f-deg').value, 10) || 0;
    var focusOn = document.getElementById('f-focus').checked;
    var vis = {{}};
    for (var i = 0; i < nodes.length; i++) {{
      var n = nodes[i], id = String(n.id);
      if ((n.degree || 0) < minDeg) continue;
      if (q && (String(n.source_file || '').toLowerCase().indexOf(q) === -1)) continue;
      vis[id] = true;
    }}
    if (focusOn && FOCUSED_ID) {{
      var keep = {{}}; keep[FOCUSED_ID] = true;
      var frontier = [FOCUSED_ID];
      for (var h = 0; h < FILTER_HOPS; h++) {{
        var next = [];
        frontier.forEach(function (f) {{
          neighborsOf(f).forEach(function (nb) {{ if (!keep[nb]) {{ keep[nb] = true; next.push(nb); }} }});
        }});
        frontier = next;
      }}
      var merged = {{}};
      for (var k in vis) {{ if (keep[k]) merged[k] = true; }}
      vis = merged;
    }}
    return vis;
  }}

  function setView(vn, ve) {{
    nodesDS.clear(); edgesDS.clear();
    nodesDS.add(vn.map(nodeObj));
    edgesDS.add(ve.map(edgeObj));
    network.setData({{ nodes: nodesDS, edges: edgesDS }});
    if (vn.length > 0 && vn.length <= PHYSICS_LIMIT) {{
      network.setOptions({{ physics: {{ enabled: true, solver: 'forceAtlas2Based', forceAtlas2Based: {{ gravitationalConstant: -60, centralGravity: 0.005, springLength: 120, springConstant: 0.08, damping: 0.4, avoidOverlap: 0.8 }}, stabilization: {{ iterations: 200, fit: true }} }} }});
      network.stabilize(200, function () {{ network.setOptions({{ physics: {{ enabled: false }} }}); }});
    }} else {{
      network.setOptions({{ physics: {{ enabled: false }} }});
    }}
    statsLine(vn, ve);
    showWarn(vn.length > PHYSICS_LIMIT ? 'Too many nodes for physics (' + vn.length + '). Narrow with filters, then Stabilize.' : '');
  }}

  function applyFilters() {{
    var vis = computeVisible();
    var vn = [], ve = [];
    nodes.forEach(function (n) {{ if (vis[String(n.id)]) vn.push(n); }});
    edges.forEach(function (e) {{ if (vis[String(e.from)] && vis[String(e.to)]) ve.push(e); }});
    setView(vn, ve);
  }}

  function hideDrill() {{ var d = document.getElementById('viz-drill'); if (d) d.parentNode.removeChild(d); }}

  function showDrill(cid) {{
    hideDrill();
    var d = DRILLDOWN[cid];
    var panel = document.createElement('div');
    panel.id = 'viz-drill';
    var total = d.count, rows = d.members;
    panel.innerHTML =
      '<h4>' + esc(d.label) + ' · ' + total + ' members</h4>' +
      '<div class="vrow"><button id="d-load" style="flex:1">Load members into graph</button><button id="d-close">Close</button></div>' +
      (rows.length < total ? '<div class="vwarn">Showing first ' + rows.length + ' of ' + total + ' members — use the path filter to narrow.</div>' : '') +
      '<input id="d-q" type="text" placeholder="Filter members…" autocomplete="off">' +
      '<div id="d-list" style="margin-top:2px"></div>';
    document.getElementById('sidebar').appendChild(panel);
    function render(q) {{
      q = (q || '').toLowerCase();
      var items = rows.filter(function (r) {{ return !q || r[1].toLowerCase().indexOf(q) !== -1 || (r[2] || '').toLowerCase().indexOf(q) !== -1; }});
      document.getElementById('d-list').innerHTML = items.slice(0, 200).map(function (r) {{
        return '<div class="drow" data-nid="' + esc(r[0]) + '"><span>' + esc(r[1]) + '</span><span class="dpath">' + esc(r[2] || '') + '</span></div>';
      }}).join('');
    }}
    render('');
    document.getElementById('d-q').addEventListener('input', function () {{ render(this.value); }});
    document.getElementById('d-close').addEventListener('click', hideDrill);
    document.getElementById('d-list').addEventListener('click', function (e) {{
      var el = e.target.closest('.drow'); if (!el) return;
      var nid = el.dataset.nid;
      if (nodesDS.get(nid)) {{ network.focus(nid, {{ scale: 1.5, animation: true }}); network.selectNodes([nid]); }}
    }});
    document.getElementById('d-load').addEventListener('click', function () {{
      var keep = {{}}; rows.forEach(function (r) {{ keep[String(r[0])] = true; }});
      var vn = nodes.filter(function (n) {{ return keep[String(n.id)]; }});
      var ve = edges.filter(function (e) {{ return keep[String(e.from)] && keep[String(e.to)]; }});
      setView(vn, ve);
      hideDrill();
      showWarn('Community loaded: ' + vn.length + ' nodes. Reset to return to the overview.');
    }});
  }}

  function bindEvents() {{
    document.getElementById('f-apply').addEventListener('click', applyFilters);
    document.getElementById('f-reset').addEventListener('click', function () {{
      document.getElementById('f-path').value = '';
      document.getElementById('f-deg').value = '0';
      document.getElementById('f-deg-v').textContent = '0';
      document.getElementById('f-focus').checked = false;
      FOCUSED_ID = null;
      hideDrill();
      // Restore the initial rendered view (meta-graph for aggregated, full
      // node view for full renders).
      setView(RAW_NODES, RAW_EDGES);
    }});
    document.getElementById('f-deg').addEventListener('input', function () {{ document.getElementById('f-deg-v').textContent = this.value; }});
    document.getElementById('f-path').addEventListener('keydown', function (e) {{ if (e.key === 'Enter') applyFilters(); }});
    document.getElementById('f-stab').addEventListener('click', function () {{
      var cur = nodesDS.length;
      if (cur === 0 || cur > PHYSICS_LIMIT) {{ showWarn('Cannot stabilize ' + cur + ' nodes — narrow the view first.'); return; }}
      network.setOptions({{ physics: {{ enabled: true, stabilization: {{ iterations: 200, fit: true }} }} }});
      network.stabilize(200, function () {{ network.setOptions({{ physics: {{ enabled: false }} }}); }});
      showWarn('');
    }});
    network.on('click', function (params) {{
      if (params.nodes && params.nodes.length) {{ FOCUSED_ID = String(params.nodes[0]); if (document.getElementById('f-focus').checked) applyFilters(); }}
    }});
    network.on('selectNode', function (params) {{ if (params.nodes && params.nodes.length) FOCUSED_ID = String(params.nodes[0]); }});
    if (Object.keys(DRILLDOWN).length) {{
      network.on('click', function (params) {{
        if (params.nodes && params.nodes.length) {{ var id = String(params.nodes[0]); if (DRILLDOWN[id]) showDrill(id); }}
      }});
    }}
  }}

  statsLine(RAW_NODES, RAW_EDGES);
  if (RAW_NODES.length > PHYSICS_LIMIT) {{
    // Halt the initial forceAtlas2 stabilization before it freezes the browser.
    if (network.stopSimulation) network.stopSimulation();
    network.setOptions({{ physics: {{ enabled: false }} }});
    showWarn('Large graph (' + RAW_NODES.length + ' nodes): physics disabled. Filter by path / degree / focus, then Stabilize.');
  }}
  bindEvents();
}})();
</script>"""


def _inject_large_viz(html_path: Path, drilldown: dict, master: tuple | None = None) -> None:
    """Append the enhanced filter/drill-down layer to a generated graph.html.

    Idempotent (guarded by the ``_VIZ_MARKER`` comment). Self-contained — the
    injected fragment reuses graphify's page-scope globals, so no forking of
    graphify is required and the structural graph.json stays authoritative.
    ``master`` = ``(nodes, edges)`` full member data to embed alongside an
    aggregated view (so drill-down/filter can rebuild a real member subgraph).
    """
    if not html_path.is_file():
        return
    text = html_path.read_text(encoding="utf-8")
    if f"<!-- {_VIZ_MARKER}" in text:
        return
    master_nodes = master[0] if master else None
    master_edges = master[1] if master else None
    frag = _viz_injection_fragment(drilldown, master_nodes=master_nodes, master_edges=master_edges)
    if "</body>" in text:
        text = text.replace("</body>", frag + "\n</body>", 1)
    else:
        text += frag
    html_path.write_text(text, encoding="utf-8")


def _html_export(graph, communities: dict[int, list[str]], output_path: Path, node_limit: int) -> str:
    """graphify's ``to_html`` + our large-viz enhancement layer.

    Returns the html status string the build callers report: ``"full"``,
    ``"aggregated"``, or ``"skipped (...)"``. The enhanced filter layer is
    injected into every rendered HTML. For the over-limit aggregated view the
    full member node/edge data is embedded alongside the community meta-graph
    so drill-down can expand a community into its member subgraph.

    Heuristic (non-LLM) community labels are passed to ``to_html`` so the
    Communities legend actually lists per-community toggles instead of only
    "Select All".
    """
    g = _graphify_imports()
    if g is None:
        return "skipped (graphifyy not installed)"
    community_labels = _community_labels(graph, communities)
    try:
        g["to_html"](
            graph,
            communities,
            str(output_path),
            node_limit=node_limit,
            community_labels=community_labels,
        )
    except ValueError as e:  # non-fatal: structural graph.json + manifest still land
        return f"skipped ({e})"
    aggregated = graph.number_of_nodes() > node_limit
    if output_path.is_file():
        master: tuple | None = None
        if aggregated:
            drilldown = _drilldown_data(graph, communities, community_labels=community_labels)
            master = _full_member_view(graph, communities, community_labels=community_labels)
        else:
            drilldown = {}
        try:
            _inject_large_viz(output_path, drilldown, master=master)
        except Exception:  # pragma: no cover - non-fatal (base viz already written)
            pass
    return "aggregated" if aggregated else "full"


# ── Family graph (graphify native global-graph mechanism) ─────────────────


def _family_codegraph_dir(slug: str) -> Path:
    """Where the merged family graph lives (config home — correct across drives)."""
    return settings.config_home / "codegraph" / f"family_{slug}"


def _load_nx_graph(graph_path: Path):
    """Load a graph.json as a networkx Graph (graphify node-link format)."""
    from networkx.readwrite import json_graph as _jg

    data = json.loads(graph_path.read_text(encoding="utf-8"))
    if "links" not in data and "edges" in data:
        data = dict(data, links=data["edges"])
    try:
        return _jg.node_link_graph(data, edges="links")
    except TypeError:
        return _jg.node_link_graph(data)


def _build_family_graph(
    slug: str,
    *,
    include_html: bool = False,
    directed: bool = False,
    out_dir: str | Path | None = None,
    node_limit: int | None = None,
) -> dict[str, Any]:
    """Build the FAMILY graph via graphify's native global-graph mechanism.

    Each member project is built with its OWN root (correct per-project
    ``source_file`` paths — never a synthetic/wrong root), then prefixed with a
    stable member tag via ``prefix_graph_for_global`` and merged into ONE graph
    at ``<config_home>/codegraph/family_<slug>/.ai/codegraph/graph.json``.

    ``out_dir`` (sandbox) redirects ALL output to a custom directory — member
    graphs to ``<out_dir>/members/<tag>/`` and the merged family to
    ``<out_dir>/family/`` — so nothing is written into the member projects
    (ideal for large real-world projects or disposable inspections).

    Correct across drives (D:/frontend + E:/backend): per-project paths stay
    clean; node IDs are tagged ``member::local``; every node carries a ``repo``
    attribute. Cross-project EDGES only form for static coupling graphify can
    see (built per-project in isolation); runtime/API calls belong in memory
    relations, not graph edges.

    Two visualizations: each member's ``graph.html`` stays its OWN per-project
    graph (built with include_html=True), while the combined ``family.html`` is
    generated in the family dir AND mirrored into every member's
    ``.ai/codegraph/family.html`` (or ``<out_dir>/members/<tag>/family.html``
    under a sandbox) so opening ``family.html`` from any member shows the same
    merged graph (to_html embeds the graph data inline, so the copy is
    self-contained).
    """
    g = _graphify_imports()
    if g is None:
        return fail_obj(error="graphifyy is not installed")
    node_limit = settings.graph_viz_limit if node_limit is None else node_limit
    render_html = bool(include_html and node_limit != 0)
    # Reconcile declared member project_ids to each project's own .ai/project-id
    # (the project is authoritative) + check for duplicate ids with different paths.
    sync_family_project_ids(slug)
    members = family_members(slug)
    if not members:
        return fail_obj(error=f"family '{slug}' has no declared members")

    import networkx as nx
    from graphify.build import prefix_graph_for_global

    combined_parts: list[nx.Graph] = []
    member_manifests: dict[str, dict[str, Any]] = {}
    member_out_dirs: dict[str, Path] = {}
    sandbox = Path(out_dir) if out_dir is not None else None
    for member in members:
        # Stable member identity (.ai/project-id authoritative > declared > derived),
        # used as the repo:: tag. Seed the marker for fresh projects (identity only).
        tag = family_member_id(slug, member)
        seed_member_project_id(slug, member)
        if sandbox is not None:
            m_out = sandbox / "members" / tag
            res = build_graph(member, include_html=True, out_dir=m_out, node_limit=node_limit)
            graph_json = m_out / "graph.json"
            member_out_dirs[tag] = m_out
        else:
            res = build_graph(member, include_html=True, node_limit=node_limit)
            graph_json = _codegraph_dir(Path(member)) / "graph.json"
            member_out_dirs[tag] = _codegraph_dir(Path(member))
        if not res.get("success"):
            return fail_obj(error=f"family member graph build failed ({tag}): {res.get('error')}")
        if not graph_json.is_file():
            return fail_obj(error=f"family member graph missing for '{tag}'")
        combined_parts.append(prefix_graph_for_global(_load_nx_graph(graph_json), tag))
        member_manifests[tag] = {
            "member": str(Path(member).resolve()),
            "source_manifest": _source_manifest(Path(member)),
        }

    combined = nx.compose_all(combined_parts)
    communities = g["cluster"](combined)
    if sandbox is not None:
        fam_out = sandbox / "family"
    else:
        fam_out = _codegraph_dir(_family_codegraph_dir(slug))
    fam_out.mkdir(parents=True, exist_ok=True)
    graph_path = fam_out / "graph.json"
    g["to_json"](
        combined,
        communities,
        str(graph_path),
        force=True,
        built_at_commit=_git_head_safe(fam_out),
    )
    artifacts = {"graph.json": str(graph_path), "family.html": ""}
    html = None
    if render_html:
        fam_html = fam_out / "family.html"
        # Same enhanced filter/drill-down layer as per-project graph.html.
        html = _html_export(combined, communities, fam_html, node_limit)
        if html in ("full", "aggregated"):
            artifacts["family.html"] = str(fam_html)
            # Mirror the merged visualization into every member location (project
            # .ai/codegraph, or the sandbox member dir) so opening family.html
            # from any member shows the SAME combined graph.
            for tag, member_out in member_out_dirs.items():
                member_out.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fam_html, member_out / "family.html")
    built_at = datetime.now(timezone.utc).isoformat()
    (fam_out / ".build_state.json").write_text(
        json.dumps(
            {
                "built_at": built_at,
                "family": slug,
                "members": {t: m["member"] for t, m in member_manifests.items()},
                "source_manifest": {t: m["source_manifest"] for t, m in member_manifests.items()},
                "nodes": combined.number_of_nodes(),
                "edges": combined.number_of_edges(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _ensure_readme(fam_out)
    return ok_obj(
        out_dir=str(fam_out),
        artifacts=artifacts,
        nodes=combined.number_of_nodes(),
        edges=combined.number_of_edges(),
        built_at=built_at,
        members=len(members),
        family_html_mirrored_to_members=bool(artifacts["family.html"]),
        node_limit=node_limit,
        html=html,
    )


def _family_status(slug: str) -> dict[str, Any]:
    """Family graph status: exists + fresh (every member's per-project graph fresh)."""
    out_dir = _codegraph_dir(_family_codegraph_dir(slug))
    state_path = out_dir / ".build_state.json"
    if not state_path.is_file() or not (out_dir / "graph.json").is_file():
        return ok_obj(exists=False, fresh=False, error="no family graph built (run graph_build family=<slug>)")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ok_obj(exists=False, fresh=False, error="corrupt family build state")
    members = family_members(slug)
    fresh = bool(members) and all(bool(graph_status(m).get("fresh")) for m in members)
    return ok_obj(
        exists=True,
        fresh=fresh,
        built_at=state.get("built_at"),
        nodes=state.get("nodes"),
        edges=state.get("edges"),
        members=list((state.get("members") or {}).keys()),
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


def _write_feedback(
    workspace_path: str | Path,
    root: Path,
    prev: dict[str, Any] | None,
    cur: dict[str, Any],
    project_id: str | None = None,
) -> None:
    """Write-time feedback: record code evolution in memory.

    On a rebuild that changed source files, write one observation to a
    ``graphify_feedback`` memory entity so the agent's memory tracks what code
    changed and when. Only fires when a previous build existed AND files changed
    (the first build is a creation, not an evolution; skipped). Never raises.
    """
    if prev is None:
        return  # first build — nothing changed yet
    changed = _changed_files(prev, cur)
    if not changed:
        return
    try:
        from .agent_recall import add_observations, ensure_entities

        preview = changed[:10]
        note = f"graphify: {len(changed)} file(s) changed at {datetime.now(timezone.utc).isoformat()}: " + ", ".join(
            preview
        )
        # Reuse an existing same-named entity — never spawn an empty duplicate.
        ensure_entities(
            workspace_path=workspace_path,
            project_id=project_id,
            names=["graphify_feedback"],
            entity_type="concept",
        )
        add_observations(
            workspace_path=workspace_path,
            project_id=project_id,
            observations=[{"entityName": "graphify_feedback", "contents": [note]}],
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the build
        pass


def _git_head_safe(root: Path) -> str:
    """Return the current git HEAD commit hash via pure file reads (no subprocess).

    graphify's ``to_json`` calls ``_git_head()`` which spawns ``git``; that
    subprocess deadlocks inside the frozen exe on Windows (``communicate``
    timeout hangs on the reader-thread join), freezing ``graph_build``. Reading
    ``.git/HEAD`` directly avoids subprocess entirely and keeps the
    ``built_at_commit`` metadata. Returns "" when not a git repo or on any error.
    """
    try:
        head = root / ".git" / "HEAD"
        if not head.is_file():
            return ""
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            ref_path = root / ".git" / ref[5:].strip()
            return ref_path.read_text(encoding="utf-8").strip()[:40] if ref_path.is_file() else ""
        return ref[:40]
    except Exception:  # noqa: BLE001 — best-effort metadata, never break the build
        return ""


def _graph_from_json(out_dir: Path) -> dict[str, Any] | None:
    """Load the prior graph.json as ``{nodes, edges}`` (or None if absent).

    graphify's JSON export stores edges under the ``links`` key (not ``edges``)
    — both are normalized here to the internal ``edges`` name.
    """
    path = out_dir / "graph.json"
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return {"nodes": d.get("nodes", []), "edges": d.get("links", d.get("edges", []))}
    except (OSError, json.JSONDecodeError):
        return None


def _module_placeholder_id(source_file: str, placeholder: str) -> str | None:
    """Derive graphify's per-module placeholder node id from a source file.

    graphify's ``extract()`` emits global placeholder nodes (empty
    ``source_file``) for unresolved types — e.g. ``any``, ``path`` — and a full
    build scopes them into per-module nodes named ``<module>_py_<name>``
    (e.g. ``src/mcp_server/helpers/response.py`` →
    ``src_mcp_server_helpers_response_py_any``). Those scoped nodes are already
    present in the prior graph, so incremental merges remap fresh edges that
    reference the global placeholder onto the matching per-module node.
    """
    if not source_file or not source_file.endswith(".py") or not placeholder:
        return None
    stem = source_file[:-3].replace("/", "_").replace(".", "_")
    return f"{stem}_py_{placeholder}"


def _merge_extractions(
    prev_graph: dict[str, Any] | None,
    fresh: dict[str, Any],
    changed: list[str],
) -> dict[str, Any]:
    """Merge fresh extraction results with the prior graph's unchanged nodes.

    Incremental rebuild: only files in ``changed`` are re-extracted (``fresh``).
    Nodes/edges belonging to unchanged files are carried over from ``prev_graph``,
    so the cross-file resolution pass never re-processes the whole corpus.

    Node identity: ``source_file`` (relative, forward-slash) + ``id``. Stale
    nodes for changed/removed files are dropped before merging fresh ones.

    graphify emits global placeholder nodes (empty ``source_file``: ``any``,
    ``path``, ...) that a full build scopes per-module (``<module>_py_any``).
    Fresh's global placeholders are stripped and fresh edges referencing them
    are remapped onto the per-module nodes carried over from ``prev_graph``;
    any edge whose endpoints can't be resolved is dropped (dangling-edge
    cleanup), so the merged output has no references to non-existent nodes.
    """
    fresh_nodes = fresh.get("nodes", [])
    fresh_edges = fresh.get("edges", [])

    if prev_graph is None:
        # First build — nothing to merge.
        return {"nodes": fresh_nodes, "edges": fresh_edges}

    changed_set = set(changed)
    prev_nodes = prev_graph.get("nodes", [])
    prev_edges = prev_graph.get("edges", [])

    # Drop nodes/edges whose source file is being re-extracted (or was removed).
    kept_nodes = [n for n in prev_nodes if (n.get("source_file") or "") not in changed_set]
    kept_edges = [e for e in prev_edges if (e.get("source_file") or "") not in changed_set]

    # Strip GLOBAL placeholder nodes from fresh (empty source_file, e.g. ``any``,
    # ``path``) UNLESS they already exist in prev_graph (per-module ``_py_any``/
    # ``_py_path`` counterparts are carried over via kept_nodes). This prevents
    # an extra global ``any`` node from drifting in on incremental builds.
    prev_placeholder_ids = {str(n.get("id") or "") for n in prev_nodes if not n.get("source_file")}
    fresh_nodes = [n for n in fresh_nodes if n.get("source_file") or str(n.get("id") or "") in prev_placeholder_ids]

    # Dedup by (source_file, id): fresh wins over carried-over.
    seen: set[tuple[str, str]] = set()
    merged_nodes: list[dict[str, Any]] = []
    for n in [*kept_nodes, *fresh_nodes]:
        key = ((n.get("source_file") or ""), str(n.get("id") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged_nodes.append(n)
    merged_ids = {str(n.get("id") or "") for n in merged_nodes}

    # Remap fresh edges that reference stripped global placeholders onto the
    # per-module scoped node; drop edges whose endpoints can't be resolved.
    remapped_edges: list[dict[str, Any]] = []
    for e in fresh_edges:
        src, tgt = str(e.get("source") or ""), str(e.get("target") or "")
        if src not in merged_ids:
            module_id = _module_placeholder_id(e.get("source_file") or "", src)
            if module_id and module_id in merged_ids:
                e = dict(e)
                e["source"] = module_id
                src = module_id
            else:
                continue  # placeholder without a scoped counterpart → drop edge
        if tgt not in merged_ids:
            module_id = _module_placeholder_id(e.get("source_file") or "", tgt)
            if module_id and module_id in merged_ids:
                e = dict(e)
                e["target"] = module_id
                tgt = module_id
            else:
                continue
        remapped_edges.append(e)

    # Final dangling-edge cleanup across ALL edges (kept + fresh): an unchanged
    # file's edge may reference a node in a changed file that was dropped, so
    # drop any edge whose endpoints no longer exist — matches the full build,
    # which emits no dangling edges.
    merged_edges = [
        e
        for e in [*kept_edges, *remapped_edges]
        if str(e.get("source") or "") in merged_ids and str(e.get("target") or "") in merged_ids
    ]
    return {"nodes": merged_nodes, "edges": merged_edges}


def _first_build_cap(max_files: int | None, chunk_size: int | None, total: int) -> int | None:
    """Leading-corpus cap for a FIRST build (min of max_files / chunk_size)."""
    caps = [c for c in (max_files, chunk_size) if c and c > 0]
    if not caps:
        return None
    return min(min(caps), total)


def build_graph(
    workspace_path: str | Path,
    root: str | Path | None = None,
    include_html: bool = True,
    directed: bool = False,
    project_id: str | None = None,
    out_dir: str | Path | None = None,
    node_limit: int | None = None,
    max_files: int | None = None,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Build the code knowledge graph (serialized per project).

    Thin lock wrapper around :func:`_build_graph_impl`: builds for the same
    project are serialized so a synchronous ``graph_build`` and a background
    auto-rebuild can never race on ``graph.json``; different projects use
    separate locks and build concurrently. ``out_dir`` redirects the graph
    artifacts (graph.json/html, .build_state.json) to a custom directory — the
    scan root stays ``root`` (useful for sandboxed family builds or keeping
    large projects untouched). Output defaults to the WORKSPACE root's
    ``.ai/codegraph`` regardless of ``root`` (which only scopes the scan).

    ``node_limit`` bounds the interactive ``graph.html`` export (default from
    ``settings.graph_viz_limit`` = 20000; ``0`` skips HTML). Every rendered
    ``graph.html`` carries an enhanced client-side layer for large graphs:
    filter by file path / min degree / 2-hop focus, and a community drill-down
    when the over-limit aggregated view is produced. ``max_files`` caps
    the corpus of the FIRST build so the initial pass on a large project is
    light. ``chunk_size`` (queue-chunk semantics, default 200) bounds EVERY run
    — each build processes at most that many files and returns ``remaining_files``
    for the next call or background worker to continue.

    The per-project lock wait is bounded (``_BUILD_LOCK_TIMEOUT``): a genuinely
    stuck build can never block a caller forever — it fails with a clear error.
    """
    lock = _build_lock(_resolve_root(workspace_path))
    if not lock.acquire(timeout=_BUILD_LOCK_TIMEOUT):
        return fail_obj(
            error=(
                "another graph build is stuck holding the build lock for this project "
                f"(>{_BUILD_LOCK_TIMEOUT:.0f}s) — restart the server or wait for it to drain"
            )
        )
    try:
        return _build_graph_impl(
            workspace_path, root, include_html, directed, project_id, out_dir, node_limit, max_files, chunk_size
        )
    finally:
        lock.release()


def graph_build_action(
    workspace_path: str | Path,
    root: str | Path | None = None,
    include_html: bool = True,
    directed: bool = False,
    project_id: str | None = None,
    family: str | None = None,
    node_limit: int | None = None,
    max_files: int | None = None,
    chunk_size: int | None = None,
    background: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Explicit ``graph_build`` — coalesces with an in-flight background rebuild.

    With ``background=True`` (the default), this is a fire-and-forget trigger:
    it starts the worker immediately and returns without processing a chunk.
    ``graph_status`` reports progress. With ``background=False``, one synchronous
    chunk is processed for callers that explicitly need blocking behavior.
    ``force=True`` bypasses the in-flight guard so a fresh build can be started
    even when a stale ``rebuilding`` flag/worker is present. ``family`` builds
    the merged family graph instead.
    """
    if family:
        return _build_family_graph(family, include_html=include_html, directed=directed, node_limit=node_limit)
    out_root = _resolve_root(workspace_path)
    # Only the fire-and-forget background trigger coalesces with an in-flight
    # worker. A synchronous build (background=False) always proceeds (Bug 3): it
    # serializes on the bounded per-project lock and returns chunk details. The
    # stale guard is also bypassed by force=True (Phase 4 escape hatch).
    if background and not force and _rebuild_in_flight(workspace_path, root):
        return ok_obj(
            success=True,
            rebuilding=True,
            out_dir=str(_codegraph_dir(out_root)),
            note="graph rebuild already in progress — will be fresh shortly; no new build started",
        )
    chunk_size = settings.graph_chunk_size if chunk_size is None else chunk_size
    if background:
        started = _background_rebuild(workspace_path, out_root, chunk_size=chunk_size)
        status = graph_status(workspace_path, root)
        return ok_obj(
            out_dir=str(_codegraph_dir(out_root)),
            background=True,
            background_started=started,
            triggered=True,
            fresh=status.get("fresh", False),
            processed_files=status.get("processed_files", 0),
            remaining_files=status.get("remaining_files"),
            note="graph build queued in background; poll graph_status for progress",
        )
    result = build_graph(
        workspace_path,
        root,
        include_html=include_html,
        directed=directed,
        project_id=project_id,
        node_limit=node_limit,
        max_files=max_files,
        chunk_size=chunk_size,
    )
    return result


def _build_graph_impl(
    workspace_path: str | Path,
    root: str | Path | None = None,
    include_html: bool = True,
    directed: bool = False,
    project_id: str | None = None,
    out_dir: str | Path | None = None,
    node_limit: int | None = None,
    max_files: int | None = None,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Build the code knowledge graph (AST-only, no LLM).

    **Incremental**: when a previous graph + manifest exist, only files whose
    mtime/size changed are re-extracted; the unchanged corpus is passed to
    graphify's cross-file resolution as read-only context (``resolution_context``)
    and merged back. This turns a full-corpus rebuild (~12s on 90 files) into a
    changed-files-only pass (~0.3s), which is what makes ``graph_fresh`` cheap
    when it auto-refreshes on every graph read.

    After a successful rebuild, writes a memory observation about changed files
    (write-time feedback) so memory tracks code evolution.

    Returns ``{success, out_dir, nodes, edges, files, built_at, incremental,
    node_limit, html, partial, pending_files}`` or an error dict.
    """
    g = _graphify_imports()
    if g is None:
        return fail_obj(error="graphifyy is not installed")

    # HTML viz limit: central config default (20000) unless explicitly set.
    # Passed EXPLICITLY to to_html so an over-limit graph renders the
    # aggregated community meta-graph view instead of raising ValueError.
    # ``0`` disables the HTML step (graphify's "disable viz" convention).
    node_limit = settings.graph_viz_limit if node_limit is None else node_limit
    render_html = bool(include_html and node_limit != 0)
    max_files = settings.graph_max_files if max_files is None else max_files
    chunk_size = settings.graph_chunk_size if chunk_size is None else chunk_size

    scan_root = _resolve_root(workspace_path, root)
    out_root = _resolve_root(workspace_path)
    out_dir = _codegraph_dir(out_root) if out_dir is None else Path(out_dir)
    # Cache follows the OUTPUT location: default → <workspace_root>/.ai/codegraph/
    # (never the scan sub-root, so root="src" cannot drop .ai inside src/); under a
    # sandbox → <out_dir>/cache so NOTHING is written into the scanned project.
    cache_root = out_root if out_dir is None else out_dir / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Capture the previous manifest BEFORE overwriting (for feedback + diff).
    prev_manifest = _load_manifest(out_dir)
    exclusions = _gitignore_exclusions(scan_root)
    cur_manifest = _source_manifest(scan_root, exclusions)
    changed = _changed_files(prev_manifest, cur_manifest)
    pending_build = bool(prev_manifest and prev_manifest.get("remaining_files", 0) > 0)
    if pending_build and not changed:
        # A partial first chunk may intentionally persist an empty or subset
        # source_manifest. It is still incomplete; resume against the current
        # filtered corpus instead of taking the fresh-skip path forever.
        changed = sorted(cur_manifest)

    try:
        detected = g["detect"](scan_root, cache_root=cache_root)

        def _default_noise_free(path: Path) -> bool:
            if _is_lock_file(path.name):
                return False  # generated dependency lock files — never graph source
            try:
                rel = path.relative_to(scan_root)
            except ValueError:
                return False  # outside scan root — not a source file
            return not _is_noise_relpath(str(rel).replace("\\", "/"))

        # graphify's detect does not honour our _NOISE_DIRS — filter scratch/temp
        # and dependency dirs (e.g. .ai/temp) out of the file list BEFORE extract.
        files_all = [Path(f) for lst in detected.get("files", {}).values() for f in lst if _default_noise_free(Path(f))]
        # Apply project .gitignore exclusions (additive; never relaxes _NOISE_DIRS).
        files = [f for f in files_all if not _gitignored(scan_root, f, exclusions)]
        if not files and files_all:
            # BLANK-DETECTION GUARD: gitignore would empty the source set — fall
            # back to the default noise rules so a misconfigured .gitignore can
            # never produce "no supported source files detected".
            files = files_all
            gitignore_fallback = True
        else:
            gitignore_fallback = False
        if not files:
            ig = detected.get("ignored") or []
            if isinstance(ig, list) and ig:
                return fail_obj(
                    error=f"no supported source files detected ({len(ig)} file(s) ignored by .gitignore/graphifyignore)"
                )
            return fail_obj(error="no supported source files detected")

        # Deterministic corpus order (stable across runs) so a ``max_files`` /
        # ``chunk_size`` cap always selects the same leading subset.
        supported_rel = {str(f.relative_to(scan_root)).replace("\\", "/") for f in files}
        total_files = len(supported_rel)
        files = sorted(files, key=lambda f: str(f.relative_to(scan_root)).replace("\\", "/"))
        rel_files = [str(f).replace("\\", "/") for f in files]
        prev_graph = _graph_from_json(out_dir) if prev_manifest else None

        # QUEUE-CHUNK SEMANTICS (Laravel-queue style): bound the work done in
        # THIS run so peak RAM/CPU stays flat on large projects. Each call
        # processes at most ``chunk_size`` files and advances the manifest by
        # exactly that chunk — a later call (or the background chunk worker)
        # picks up where this one stopped until ``remaining_files`` hits 0.
        # ``max_files`` remains the FIRST-build leading-subset cap (back-compat
        # with partial builds); the effective first cap is min(max_files,
        # chunk_size).
        chunked = False
        remaining_files = 0
        to_extract: list[Path] = []
        pending_offset = 0
        if pending_build and prev_manifest and not prev_manifest.get("source_manifest"):
            pending_offset = max(0, int(prev_manifest.get("processed_files", 0) or 0))
        if prev_graph is None:
            first_cap = _first_build_cap(max_files, chunk_size, len(files))
            if first_cap is not None and first_cap < len(files):
                chunked = True
                files = files[:first_cap]
                rel_files = [str(f).replace("\\", "/") for f in files]
        else:
            changed_set = set(changed)
            to_extract = [f for f in files if str(f.relative_to(scan_root)).replace("\\", "/") in changed_set]
            if pending_offset:
                to_extract = to_extract[pending_offset:]
            if chunk_size and len(to_extract) > chunk_size:
                to_extract = to_extract[:chunk_size]
                chunked = True
        # Files actually processed THIS run (relative relpaths).
        processed_rel = {
            str(f.relative_to(scan_root)).replace("\\", "/") for f in (to_extract if prev_graph else files)
        }
        # Supported source files still needing extraction after this run.
        changed_supported = (set(changed) & supported_rel) if prev_graph else supported_rel
        if pending_offset:
            remaining_files = max(0, total_files - pending_offset - len(processed_rel))
        else:
            remaining_files = max(0, len(changed_supported - processed_rel))

        # Fresh-skip: prior graph exists AND nothing changed → nothing to rebuild.
        # Makes no-op rebuilds (e.g. a graph_fresh precondition re-check) instant
        # instead of re-running a warm full-corpus extract. Still self-heal a
        # graph built before the Vite-alias pass (or by an older version) so
        # stale graphs gain alias-import edges on the next read.
        if prev_graph is not None and not changed and not pending_build:
            alias_edges = _augment_alias_import_edges_json(out_dir / "graph.json", scan_root)
            artifacts = {"graph.json": str(out_dir / "graph.json")}
            html = None
            if render_html and (out_dir / "graph.html").is_file():
                artifacts["graph.html"] = str(out_dir / "graph.html")
                html = "existing"
            return ok_obj(
                out_dir=str(out_dir),
                artifacts=artifacts,
                nodes=len(prev_graph["nodes"]),
                edges=len(prev_graph["edges"]),
                files=len(rel_files),
                built_at=(prev_manifest or {}).get("built_at"),
                incremental=False,
                skipped=True,
                alias_edges=alias_edges,
                node_limit=node_limit,
                html=html,
            )

        # Extraction: incremental (chunked or not) when a prior graph exists,
        # full when first building. A chunked incremental merge carries ONLY the
        # processed chunk — pending files keep their old nodes so the graph stays
        # readable while catch-up proceeds.
        if prev_graph is not None and changed:
            assert prev_graph is not None
            if chunked:
                fresh = g["extract"](
                    to_extract,
                    root=scan_root,
                    cache_root=cache_root,
                    parallel=_use_parallel(),
                    resolution_context_nodes=prev_graph["nodes"],
                    resolution_context_edges=prev_graph["edges"],
                )
                extraction = _merge_extractions(prev_graph, fresh, sorted(processed_rel))
                incremental = True
            elif len(changed) < len(rel_files):
                # Original non-chunked incremental: re-extract all changed files.
                fresh = g["extract"](
                    to_extract,
                    root=scan_root,
                    cache_root=cache_root,
                    parallel=_use_parallel(),
                    resolution_context_nodes=prev_graph["nodes"],
                    resolution_context_edges=prev_graph["edges"],
                )
                extraction = _merge_extractions(prev_graph, fresh, changed)
                incremental = True
            else:
                # Full rebuild (first time, or nearly everything changed).
                extraction = g["extract"](files, root=scan_root, cache_root=cache_root, parallel=_use_parallel())
                incremental = False
        else:
            # First build (the nothing-changed case was handled by fresh-skip).
            extraction = g["extract"](files, root=scan_root, cache_root=cache_root, parallel=_use_parallel())
            incremental = False

        graph = g["build_from_json"](extraction, root=scan_root, directed=directed)
        communities = g["cluster"](graph)

        # Vite/JS path-alias augmentation: graphifyy cannot resolve
        # ``'@/...'`` imports (its alias cache only reads tsconfig/jsconfig
        # paths), so add the missing imports_from/imports edges here so .vue
        # SFCs and other alias-importing files stay connected in graph_path.
        alias_edges = _augment_alias_import_edges(graph, scan_root)

        graph_path = out_dir / "graph.json"
        # ``force``: on an incremental rebuild a smaller graph is legitimate
        # (a file was deleted, or symbols were removed from a changed file) —
        # graphify's shrink guard would otherwise refuse to overwrite and stale
        # nodes would persist. The merge is fully controlled (unchanged files
        # carried over from the prior graph), so the reduction is trusted here.
        # ``built_at_commit`` is passed explicitly so graphify skips its
        # subprocess ``git`` call, which deadlocks in the frozen exe (see
        # _git_head_safe).
        # ``force``: a chunked build legitimately produces a smaller intermediate
        # graph (it grows as chunks land) — the shrink guard must NOT refuse to
        # overwrite an existing larger graph.json, or the partial chunk never
        # lands and graph.json stays stale while the manifest advances. Any
        # changed source/exclusion set is also an intentional replacement, so
        # deletions and .graphignore edits may legitimately shrink the graph.
        g["to_json"](
            graph,
            communities,
            str(graph_path),
            force=bool(changed or incremental or chunked),
            built_at_commit=_git_head_safe(out_root),
        )

        artifacts = {"graph.json": str(graph_path)}
        html = None
        # Render the interactive graph.html only when the graph is COMPLETE
        # (non-chunked, or the final chunk). Re-embedding the whole (large)
        # merged graph into HTML on every intermediate chunk is very slow and
        # makes chunked builds appear stuck on big projects.
        if render_html and (not chunked or remaining_files == 0):
            html_path = out_dir / "graph.html"
            # Explicit node_limit → over-limit graphs render the aggregated
            # community meta-graph view (graceful) instead of raising; the
            # enhanced filter/drill-down layer is injected into either view.
            html = _html_export(graph, communities, html_path, node_limit)
            if html in ("full", "aggregated"):
                artifacts["graph.html"] = str(html_path)

        # Chunked builds advance the manifest ONLY for the files processed this
        # run: pending supported files keep their previous signature (so they
        # stay "changed"/"added" and are picked up next run), deleted files drop
        # out, and non-source files are recorded complete so status is accurate.
        if chunked:
            prev_src = (prev_manifest or {}).get("source_manifest", {})
            manifest_src = dict(cur_manifest)
            for rel in changed_supported - processed_rel:
                if rel in prev_src:
                    manifest_src[rel] = prev_src[rel]
                else:
                    manifest_src.pop(rel, None)
        else:
            manifest_src = cur_manifest

        built_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "built_at": built_at,
            "scan_root": str(scan_root),
            "output_root": str(out_root),
            "total_files": total_files,
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "processed_files": len(processed_rel),
            "remaining_files": remaining_files,
            "source_manifest": manifest_src,
        }
        if chunked:
            manifest["chunked"] = True
        # Carry the worker-lifecycle flags forward so a chunk write can never
        # clobber an in-flight rebuilding marker (started/last-progress/error).
        for k in _REBUILD_STATE_KEYS:
            if prev_manifest and k in prev_manifest:
                manifest[k] = prev_manifest[k]
        (out_dir / ".build_state.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _ensure_readme(out_dir)

        # Write-time feedback: record changed files in memory.
        _write_feedback(workspace_path, scan_root, prev_manifest, manifest, project_id)

        return ok_obj(
            out_dir=str(out_dir),
            artifacts=artifacts,
            nodes=graph.number_of_nodes(),
            edges=graph.number_of_edges(),
            files=len(processed_rel) if chunked else len(rel_files),
            total_files=total_files,
            processed_files=len(processed_rel),
            remaining_files=remaining_files,
            built_at=built_at,
            incremental=incremental,
            gitignore_fallback=gitignore_fallback,
            alias_edges=alias_edges,
            node_limit=node_limit,
            html=html,
            chunked=chunked,
            partial=bool(chunked and prev_graph is None),
            pending_files=remaining_files,
        )
    except Exception as e:  # noqa: BLE001 — loud, actionable
        return fail_obj(error=f"graph build failed: {e}")


def _ensure_readme(out_dir: Path) -> None:
    """Write a README marking .ai/codegraph/ as generated + safe to delete."""
    readme = out_dir / "README.md"
    if readme.exists():
        return
    readme.write_text(
        "# codegraph\n\n"
        "Generated by `graph_build` (graphifyy, AST-only — no LLM). Safe to delete; "
        'rebuild with `action_call(action="graph_build", params={"workspace_path": "..."})`.\n'
        "\n\n"
        "- `graph.json` — the code knowledge graph\n"
        "- `graph.html` — interactive visualization (open in a browser)\n"
        "- `.build_state.json` — freshness manifest (mtime/size per source file)\n"
        "- `cache/` — graphify's own extraction cache (regenerable; safe to delete)\n"
        "\n"
        "## Exclusions (what the graph indexes)\n"
        "\n"
        "Files/directories are excluded from the graph using gitignore syntax, additive:\n"
        "- `_NOISE_DIRS` (`.git`, `.venv`, `node_modules`, `dist`, `build`, `vendor`, ...) and "
        "dependency lock files — always excluded\n"
        "- project `.gitignore` — always honored\n"
        "- `.graphignore` — graph-only exclusions; NEVER affects git. Add generated code, "
        "vendored copies, etc. here so they stay out of the graph without touching `.gitignore`.\n",
        encoding="utf-8",
    )


def graph_status(
    workspace_path: str | Path, root: str | Path | None = None, family: str | None = None
) -> dict[str, Any]:
    """Return whether the graph exists and is fresh (source unchanged since last build)."""
    if family:
        return _family_status(family)
    scan_root = _resolve_root(workspace_path, root)
    out_dir = _codegraph_dir(_resolve_root(workspace_path))
    state_path = out_dir / ".build_state.json"

    if not state_path.is_file() or not (out_dir / "graph.json").is_file():
        return ok_obj(exists=False, fresh=False, error="no graph built")

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ok_obj(exists=False, fresh=False, error="corrupt build state")

    prev = state.get("source_manifest", {})
    cur = _source_manifest(scan_root, _gitignore_exclusions(scan_root))
    changed = sorted(p for p in cur if prev.get(p) != cur.get(p))
    removed = sorted(p for p in prev if p not in cur)
    pending_build = bool(state.get("remaining_files", 0) > 0)
    fresh = not changed and not removed and not pending_build

    # Worker lifecycle: a live (progressing) worker is "rebuilding". A persisted
    # `rebuilding: true` with NO live worker is stale (worker died / server
    # restarted) — auto-clear it so status never reports a phantom rebuild.
    live = _rebuild_in_flight(workspace_path, root)
    persisted_rebuilding = bool(state.get("rebuilding"))
    if persisted_rebuilding and not live:
        _manifest_update(out_dir, rebuilding=False)
        persisted_rebuilding = False

    total = max(len(cur), int(state.get("total_files", 0) or 0))
    remaining = int(state.get("remaining_files", 0) or 0)
    processed_total = max(0, total - remaining)

    # Exclusion visibility: how much of the scanned corpus the project rules
    # (.gitignore + .graphignore + _NOISE_DIRS) actually exclude from the graph.
    supported_files = len(cur)
    scanned_files = _scanned_count(scan_root)
    excluded_files = max(0, scanned_files - supported_files)

    return ok_obj(
        exists=True,
        fresh=fresh,
        built_at=state.get("built_at"),
        nodes=state.get("nodes"),
        edges=state.get("edges"),
        total_files=total,
        processed_files=processed_total,  # cumulative (total - remaining)
        processed_total=processed_total,
        processed_this_chunk=state.get("processed_files"),
        remaining_files=remaining,
        chunked=bool(state.get("chunked")),
        rebuilding=bool(live or persisted_rebuilding),
        rebuilding_started_at=state.get("rebuilding_started_at"),
        rebuilding_last_progress_at=state.get("rebuilding_last_progress_at"),
        scanned_files=scanned_files,
        excluded_files=excluded_files,
        supported_files=supported_files,
        background_error=_BACKGROUND_ERRORS.get(str(_resolve_root(workspace_path))) or state.get("rebuilding_error"),
        changed_files=changed[:20],
        removed_files=removed[:10],
    )


def _graph_read_pending(
    workspace_path: str | Path,
    root: str | Path | None = None,
    family: str | None = None,
) -> dict[str, Any] | None:
    """Return a pending response when graph reads would serve incomplete nodes."""
    if family:
        return None
    status = graph_status(workspace_path, root)
    if status.get("fresh") and not status.get("remaining_files"):
        return None
    return ok_obj(
        count=0,
        results=[],
        mode="pending",
        graph_pending=True,
        note="graph build is still in progress; retry after graph_status reports fresh=true",
        **_graph_freshness(workspace_path, root, family=family),
    )


def ensure_fresh(
    workspace_path: str | Path,
    root: str | Path | None = None,
    *,
    background: bool = False,
    family: str | None = None,
    chunk_size: int | None = None,
) -> dict[str, Any]:
    """Idempotent freshness guarantee: rebuild only when missing or stale.

    Returns ``{success, fresh, updated}`` where ``updated`` is 1 if a rebuild ran.

    With ``background=True``, a stale-but-existing graph defers a HEAVY rebuild
    (first build, or >= ``_BACKGROUND_THRESHOLD`` changed files) to a background
    thread and returns immediately with ``{fresh: False, background: True}`` —
    the current graph.json stays readable (atomic writes) and the next read sees
    fresh data. Small incremental rebuilds stay synchronous so a read right after
    an edit returns accurate results. A first build (no graph yet) is always
    synchronous because there is nothing to read otherwise. ``family`` ensures
    the merged family graph: per-member per-project graphs are kept fresh, then
    the family merge is rebuilt when missing or stale.
    """
    if family:
        st = _family_status(family)
        if st.get("fresh"):
            return ok_obj(fresh=True, updated=0, exists=True)
        for m in family_members(family):
            res = build_graph(m, include_html=False)
            if not res.get("success"):
                return fail_obj(error=res.get("error", "family member build failed"))
        result = _build_family_graph(family)
        result["fresh"] = True
        result["updated"] = 1 if result.get("success") else 0
        return result
    st = graph_status(workspace_path, root)
    if st.get("fresh"):
        return ok_obj(fresh=True, updated=0, exists=st.get("exists"))
    if background and st.get("exists"):
        r = _resolve_root(workspace_path)
        prev_manifest = _load_manifest(_codegraph_dir(r))
        scan_root = _resolve_root(workspace_path, root)
        changed = (
            _changed_files(prev_manifest, _source_manifest(scan_root, _gitignore_exclusions(scan_root)))
            if prev_manifest
            else []
        )
        if prev_manifest is None or len(changed) >= _BACKGROUND_THRESHOLD:
            # Queue-chunk completion: a background worker keeps advancing bounded
            # chunks until the graph is complete (smooth on large projects).
            chunk_size = settings.graph_chunk_size if chunk_size is None else chunk_size
            started = _background_rebuild(workspace_path, r, chunk_size=chunk_size)
            return ok_obj(fresh=False, background=True, started=started, updated=0, exists=True)
    result = build_graph(workspace_path, root)
    result["fresh"] = True
    result["updated"] = 1 if result.get("success") else 0
    return result


# ══════════════════════════════════════════════════════════════════════════
# ── Read queries (deterministic, over graph.json — no LLM) ────────────────
# ══════════════════════════════════════════════════════════════════════════


def _load_graph(workspace_path: str | Path, root: str | Path | None = None) -> dict[str, Any] | None:
    """Load the built graph.json, or None if it doesn't exist.

    An ABSOLUTE ``root`` (e.g. the family graph dir from ``_resolve_graph_root``)
    is honored as the output location; a relative ``root`` (scan scope like
    ``"src"``) or None resolves to the WORKSPACE root's ``.ai/codegraph`` — so
    ``root="src"`` can never read/drop a graph inside ``src/``.
    """
    if root is not None and Path(root).is_absolute():
        out_dir = _codegraph_dir(Path(root))
    else:
        out_dir = _codegraph_dir(_resolve_root(workspace_path))
    graph_path = out_dir / "graph.json"
    if not graph_path.is_file():
        return None
    try:
        return json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _find_node_id(data: dict[str, Any], term: str) -> str | None:
    """Resolve a node by id OR label, in order of specificity:

    0. Exact node ``id`` — so ids returned by ``graph_query`` (e.g.
       ``src_stores_auth_useauthstore``) work unchanged in ``graph_path`` /
       ``graph_explain``. This is what makes node identity CONSISTENT across
       query / explain / path.
    1. Exact label match (case-insensitive).
    2. ``name`` field exact match (some exporters split label/name).
    3. Source-file match — a module path (e.g. ``src/stores/auth.js``) resolves
       to its file/module node, so ``graph_path`` works with file paths directly
       (cross-file navigation). Prefers the module node (label == basename); a
       path-style term also matches by SUFFIX so full paths work regardless of
       the build-root prefix (e.g. ``mcp_server/helpers/agent_recall.py`` matches
       ``src/mcp_server/helpers/agent_recall.py``).
    4. Function-name exact match — the term matches a label after stripping a
       trailing ``()``/``(...)`` call signature (e.g. ``graph_status`` matches
       the node labeled ``graph_status()``). Without this, ``graph_path`` could
       resolve ``graph_status`` to an unrelated node whose label merely
       *contains* the term (e.g. a test named ``test_graph_status_...``).
    5. First substring label match (fallback).
    6. Substring id match (last-resort fallback).
    """
    raw = (term or "").strip()
    if not raw:
        return None
    t = raw.lower()
    nodes = data.get("nodes", [])
    for n in nodes:
        if str(n.get("id") or "") == raw:
            return n.get("id")
    for n in nodes:
        if (n.get("label") or "").lower() == t:
            return n.get("id")
    for n in nodes:
        if (n.get("name") or "").lower() == t:
            return n.get("id")
    for n in nodes:
        src = (n.get("source_file") or "").lower()
        if src == t and (n.get("label") or "").lower() == src.rsplit("/", 1)[-1]:
            return n.get("id")
    for n in nodes:
        if (n.get("source_file") or "").lower() == t:
            return n.get("id")
    # Source-file SUFFIX fallback: a path-style term should resolve regardless of
    # the build-root prefix (e.g. "mcp_server/helpers/agent_recall.py" matches a
    # graph whose source_file is "src/mcp_server/helpers/agent_recall.py").
    for n in nodes:
        src = (n.get("source_file") or "").lower()
        if "/" in t and src.endswith(t):
            return n.get("id")
    for n in nodes:
        label = (n.get("label") or "").lower()
        if label.split("(", 1)[0].strip() == t:
            return n.get("id")
    for n in nodes:
        if t in (n.get("label") or "").lower():
            return n.get("id")
    for n in nodes:
        if t in str(n.get("id") or "").lower():
            return n.get("id")
    return None


# ── Read-time correlation: code node ↔ agent memory ─────────────────────────


def _related_memory(
    workspace_path: str | Path,
    term: str,
    source_file: str | None = None,
    project_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find memory entities related to a code symbol.

    Searches agent-recall memory by the symbol label and its source file (the
    correlation key: graph node ``source_file`` + label ↔ memory entity).
    Never raises — correlation is a best-effort enrichment, and a memory search
    failure must not break the graph read.
    """
    try:
        from .agent_recall import search_nodes

        terms = [t for t in (term, source_file) if t]
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for t in terms:
            if not t:
                continue
            for ent in search_nodes(workspace_path=workspace_path, project_id=project_id, query=t, limit=limit):
                name = ent.get("name") or ent.get("entity_name") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                results.append(
                    {
                        "name": name,
                        "observations": (ent.get("observations") or [])[:5],
                    }
                )
                if len(results) >= limit:
                    return results
        return results
    except Exception:  # noqa: BLE001 — best-effort, never break the graph read
        return []


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
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not d.endswith(".egg-info")]
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        parent = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if not exclusions.excludes_dir(parent, d)]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")) or _is_lock_file(name):
                continue
            path = Path(dirpath) / name
            if _gitignored(root, path, exclusions):
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


def query_graph(
    workspace_path: str | Path,
    query: str,
    limit: int = 10,
    root: str | Path | None = None,
    project_id: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Search graph nodes by label / source file / type (case-insensitive, ranked).

    Appends ``related_memory`` (memory entities matching the query term / source
    files) so the agent sees code + memory together (read-time correlation).
    """
    root = _resolve_graph_root(workspace_path, root, family)
    pending = _graph_read_pending(workspace_path, root, family=family)
    if pending is not None:
        return pending
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")
    q = (query or "").strip().lower()
    if not q:
        return fail_obj(error="query required")

    scored: list[tuple[float, dict[str, Any]]] = []
    for n in data.get("nodes", []):
        label = (n.get("label") or "").lower()
        source = (n.get("source_file") or "").lower()
        ntype = (n.get("type") or "").lower()
        if label == q:
            score = 3.0
        elif q in label:
            score = 2.0
        elif q in source:
            score = 1.0
        elif q in ntype:
            score = 0.5
        else:
            continue
        scored.append((score, n))

    scored.sort(key=lambda x: (-x[0], (x[1].get("label") or "").lower()))
    results = [
        {
            "id": n.get("id"),
            "label": n.get("label"),
            "type": n.get("type"),
            "source_file": n.get("source_file"),
            "repo": n.get("repo"),
        }
        for _, n in scored[: max(1, int(limit or 10))]
    ]
    related_memory = _related_memory(workspace_path, q, project_id=project_id, limit=5)

    if not results:
        # Identifier fallback: the term exists in source but is not a graph node
        # (computed/ref/prop/local variables). Return whole-word source hits so
        # the query is not a dead end — mode distinguishes these from node hits.
        fallback = _identifier_scan(
            workspace_path, _resolve_root(workspace_path, root), q, limit=max(1, int(limit or 10))
        )
        if fallback:
            return ok_obj(
                count=len(fallback),
                results=fallback,
                mode="identifier",
                note=(
                    "no graph node for this term — returned whole-word identifier "
                    "matches from source (graph indexes file/function/component labels only)"
                ),
                related_memory=related_memory,
                **_graph_freshness(workspace_path, root, family=family),
            )

    return ok_obj(
        count=len(scored),
        results=results,
        mode="node",
        related_memory=related_memory,
        **_graph_freshness(workspace_path, root, family=family),
    )


def _bfs_path(adj: dict[str, list[str]], start: str, target: str) -> list[str] | None:
    """BFS shortest path from ``start`` to ``target`` over adjacency, or None."""
    if start == target:
        return [start]
    frontier = deque([start])
    prev: dict[str, str | None] = {start: None}
    while frontier:
        cur = frontier.popleft()
        if cur == target:
            break
        for nb in adj.get(cur, []):
            if nb not in prev:
                prev[nb] = cur
                frontier.append(nb)
    if target not in prev:
        return None
    ids: list[str] = []
    cur: str | None = target
    while cur is not None:
        ids.append(cur)
        cur = prev[cur]
    ids.reverse()
    return ids


def _module_nodes(data: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """Map ``source_file`` -> module node id, plus the set of module node ids.

    graphify's per-file module nodes have ``label`` == basename of their
    ``source_file`` (e.g. ``DashboardPage.vue`` for ``src/pages/DashboardPage.vue``).
    Module-level edges (``imports_from`` / ``imports``) connect these file nodes,
    which is what lets ``graph_path`` fall back to a file-level path when no
    symbol-level path exists (e.g. a component that imports a store).
    """
    file_to_module: dict[str, str] = {}
    module_ids: set[str] = set()
    for n in data.get("nodes", []):
        src = n.get("source_file") or ""
        label = n.get("label") or ""
        if src and label and label == src.rsplit("/", 1)[-1]:
            file_to_module.setdefault(src, n.get("id"))
            module_ids.add(n.get("id"))
    return file_to_module, module_ids


def path_query(
    workspace_path: str | Path,
    a: str,
    b: str,
    root: str | Path | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Shortest path (BFS) between two nodes — by id OR label, symbol-first.

    Symbol-level BFS first; if no path, falls back to module/file-level BFS
    (cross-file import relationships). Rich failure diagnostics: distinguishes
    "node(s) not found" from "no path found", and on no-path reports both source
    files plus whether a module relationship exists. ``family`` queries the
    family graph spanning correlated member projects.
    """
    root = _resolve_graph_root(workspace_path, root, family)
    pending = _graph_read_pending(workspace_path, root, family=family)
    if pending is not None:
        return pending
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")

    a_id, b_id = _find_node_id(data, a), _find_node_id(data, b)
    if a_id is None or b_id is None:
        missing = [t for t, i in ((a, a_id), (b, b_id)) if i is None]
        return fail_obj(error=f"node(s) not found: {', '.join(missing)}")

    label = {n.get("id"): n.get("label") for n in data.get("nodes", [])}
    node_info = {n.get("id"): n for n in data.get("nodes", [])}

    adj: dict[str, list[str]] = {}
    for link in data.get("links", []):
        s, tgt = link.get("source"), link.get("target")
        adj.setdefault(s, []).append(tgt)
        adj.setdefault(tgt, []).append(s)

    path = _bfs_path(adj, a_id, b_id)
    mode = "symbol"
    if path is None:
        # Module-level fallback: the two symbols live in files that may be
        # connected by an import (e.g. DashboardPage.vue imports the auth store).
        file_to_module, module_ids = _module_nodes(data)
        src_a = (node_info.get(a_id) or {}).get("source_file") or ""
        src_b = (node_info.get(b_id) or {}).get("source_file") or ""
        ma, mb = file_to_module.get(src_a), file_to_module.get(src_b)
        if ma is not None and mb is not None:
            module_adj: dict[str, list[str]] = {}
            for link in data.get("links", []):
                s, tgt = link.get("source"), link.get("target")
                if s in module_ids and tgt in module_ids:
                    module_adj.setdefault(s, []).append(tgt)
                    module_adj.setdefault(tgt, []).append(s)
            module_path = _bfs_path(module_adj, ma, mb)
            if module_path is not None:
                path = module_path
                mode = "module"

    if path is None:
        # Diagnostics: both nodes exist but are disconnected. Report source
        # files + whether ANY module-level relationship exists between them.
        src_a = (node_info.get(a_id) or {}).get("source_file") or "?"
        src_b = (node_info.get(b_id) or {}).get("source_file") or "?"
        return fail_obj(
            error="no path found between the two nodes",
            no_path=True,
            a={"label": label.get(a_id, a), "source_file": src_a},
            b={"label": label.get(b_id, b), "source_file": src_b},
            hint=(f"no symbol path or module-level (imports_from/imports) connection between {src_a} and {src_b}"),
        )

    result: dict[str, Any] = ok_obj(
        path=[label.get(i, i) for i in path],
        hops=max(0, len(path) - 1),
        mode=mode,
        **_graph_freshness(workspace_path, root, family=family),
    )
    if mode == "module":
        result["note"] = "path found at module/file level (cross-file import)"
    return result


def explain_node(
    workspace_path: str | Path,
    node: str,
    limit: int = 30,
    root: str | Path | None = None,
    project_id: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Explain a node: its details + direct neighbours with relation types.

    Appends ``related_memory`` (memory entities matching the symbol label and/or
    its source file) so the agent sees code + memory together.
    """
    root = _resolve_graph_root(workspace_path, root, family)
    pending = _graph_read_pending(workspace_path, root, family=family)
    if pending is not None:
        return pending
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")

    node_id = _find_node_id(data, node)
    if node_id is None:
        return fail_obj(error=f"node '{node}' not found")

    target = next((n for n in data.get("nodes", []) if n.get("id") == node_id), None)
    if target is None:
        return fail_obj(error=f"node '{node}' not found")

    neighbours: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for link in data.get("links", []):
        if link.get("source") == node_id:
            nid, rel = link.get("target"), link.get("relation")
        elif link.get("target") == node_id:
            nid, rel = link.get("source"), link.get("relation")
        else:
            continue
        key = (nid, rel)
        if key in seen:
            continue
        seen.add(key)
        neighbours.append({"node": nid, "relation": rel})
        if len(neighbours) >= max(1, int(limit or 30)):
            break

    label = {n.get("id"): n.get("label") for n in data.get("nodes", [])}
    return ok_obj(
        node={
            "id": target.get("id"),
            "label": target.get("label"),
            "type": target.get("type"),
            "source_file": target.get("source_file"),
            "source_location": target.get("source_location"),
        },
        neighbours=[{"node": label.get(x["node"], x["node"]), "relation": x["relation"]} for x in neighbours],
        related_memory=_related_memory(
            workspace_path,
            target.get("label") or node,
            source_file=target.get("source_file"),
            project_id=project_id,
            limit=5,
        ),
        **_graph_freshness(workspace_path, root, family=family),
    )
