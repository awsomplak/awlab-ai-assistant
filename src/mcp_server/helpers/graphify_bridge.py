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
_BACKGROUND_THREADS: dict[str, threading.Thread] = {}


def _build_lock(root: Path) -> threading.RLock:
    """Return the per-project reentrant lock (one per resolved workspace root)."""
    key = str(Path(root).resolve())
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault(key, threading.RLock())


def _background_rebuild(workspace_path: str | Path, root: Path) -> bool:
    """Start a background rebuild for the project (single-flight per workspace).

    Returns True if a new background thread was started, False if one is already
    in flight for this project. The thread runs ``build_graph`` (which takes the
    per-project lock) and never raises — a failed background build must not take
    the server down; the next read will simply retry.
    """
    key = str(Path(root).resolve())
    with _BUILD_LOCKS_GUARD:
        existing = _BACKGROUND_THREADS.get(key)
        if existing is not None and existing.is_alive():
            return False

        def _run() -> None:
            try:
                build_graph(workspace_path, root)
            except Exception:  # noqa: BLE001 — background, never crash the server
                pass
            finally:
                with _BUILD_LOCKS_GUARD:
                    _BACKGROUND_THREADS.pop(key, None)

        t = threading.Thread(target=_run, name=f"graph-rebuild-{key[-24:]}", daemon=True)
        _BACKGROUND_THREADS[key] = t
        t.start()
        return True


def _rebuild_in_flight(workspace_path: str | Path, root: str | Path | None = None) -> bool:
    """True if a background rebuild is currently running for this project."""
    key = str(_resolve_root(workspace_path, root))
    with _BUILD_LOCKS_GUARD:
        t = _BACKGROUND_THREADS.get(key)
    return t is not None and t.is_alive()


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
            if name.endswith((".pyc", ".pyo")) or _is_lock_file(name):
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
    return manifest


# ── Project .gitignore-aware exclusion (ADDITIVE to _NOISE_DIRS) ───────────


@dataclass
class _ProjectExclusions:
    """Project-derived exclusion rules (parsed from .gitignore files).

    gitignore is a SUPPLEMENT on top of the ALWAYS-applied ``_NOISE_DIRS`` safety
    net — it can exclude MORE (project junk: dist/, build/, coverage/, ...) but
    can NEVER re-include a ``_NOISE_DIRS`` path. Only positive patterns are
    applied (``!`` negations are ignored — conservative), and a blank-detection
    guard keeps gitignore from ever emptying the source set.
    """

    dir_names: set[str] = field(default_factory=set)  # any-level directory names (O(1) prune)
    dir_paths: set[str] = field(default_factory=set)  # anchored relative dir paths (fwd-slash)
    name_globs: list[str] = field(default_factory=list)  # basename globs (e.g. *.log)
    path_globs: list[str] = field(default_factory=list)  # relative-path globs

    def excludes_dir(self, parent_rel: str, name: str) -> bool:
        """True when a directory (name under parent_rel) should be pruned."""
        if name in self.dir_names:
            return True
        if parent_rel:
            return f"{parent_rel}/{name}" in self.dir_paths
        return name in self.dir_paths

    def excludes_file(self, rel: str, name: str) -> bool:
        """True when a file (rel path + basename) should be excluded."""
        if name in self.dir_names:
            return True
        if any(fnmatch.fnmatch(name, g) for g in self.name_globs):
            return True
        return any(fnmatch.fnmatch(rel, g) for g in self.path_globs)


def _parse_gitignore(ex: _ProjectExclusions, content: str, base_rel: str) -> None:
    """Parse one .gitignore into the exclusion set (base_rel = dir rel to scan root)."""
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue  # skip blanks, comments, and negations (conservative)
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1].strip()
        anchored = line.startswith("/")
        if anchored:
            line = line[1:].strip()
        if not line:
            continue
        if anchored or "/" in line:
            rel = f"{base_rel}/{line}" if base_rel else line
            rel = rel.strip("/")
            if dir_only:
                ex.dir_paths.add(rel)
            else:
                ex.path_globs.append(rel)
        else:
            ex.dir_names.add(line)  # plain name → prunes a dir OR excludes a file with that name
            if any(ch in line for ch in "*?["):
                ex.name_globs.append(line)


_EXCLUSION_CACHE: dict[str, tuple[float, _ProjectExclusions]] = {}
_EXCLUSION_TTL = 30.0  # seconds — re-walk after this so gitignore edits are picked up


def _gitignore_exclusions(scan_root: Path) -> _ProjectExclusions:
    """Load all .gitignore-derived exclusions under scan_root (cached with a TTL).

    The walk prunes ``_NOISE_DIRS`` so dependency/junk subtrees are never
    traversed just to read gitignores — this keeps the extra pass cheap while
    the returned dir-name set makes later scans skip even more.
    """
    key = str(Path(scan_root).resolve())
    now = time.monotonic()
    cached = _EXCLUSION_CACHE.get(key)
    if cached is not None and now - cached[0] < _EXCLUSION_TTL:
        return cached[1]
    ex = _ProjectExclusions()
    root = Path(scan_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not d.endswith(".egg-info")]
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        if ".gitignore" in filenames:
            try:
                content = (Path(dirpath) / ".gitignore").read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = ""
            _parse_gitignore(ex, content, "" if rel_dir == "." else rel_dir)
    _EXCLUSION_CACHE[key] = (now, ex)
    return ex


def _gitignored(scan_root: Path, path: Path, ex: _ProjectExclusions) -> bool:
    """True when a file path is excluded by project gitignore rules (dirs included)."""
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
            res = build_graph(member, include_html=True, out_dir=m_out)
            graph_json = m_out / "graph.json"
            member_out_dirs[tag] = m_out
        else:
            res = build_graph(member, include_html=True)
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
    if include_html:
        fam_html = fam_out / "family.html"
        g["to_html"](combined, communities, str(fam_html))
        artifacts["family.html"] = str(fam_html)
        # Mirror the merged visualization into every member location (project
        # .ai/codegraph, or the sandbox member dir) so opening family.html from
        # any member shows the SAME combined graph.
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
        family_html_mirrored_to_members=bool(include_html),
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


def build_graph(
    workspace_path: str | Path,
    root: str | Path | None = None,
    include_html: bool = True,
    directed: bool = False,
    project_id: str | None = None,
    out_dir: str | Path | None = None,
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
    """
    with _build_lock(_resolve_root(workspace_path)):
        return _build_graph_impl(workspace_path, root, include_html, directed, project_id, out_dir)


def graph_build_action(
    workspace_path: str | Path,
    root: str | Path | None = None,
    include_html: bool = True,
    directed: bool = False,
    project_id: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Explicit ``graph_build`` — coalesces with an in-flight background rebuild.

    If a background rebuild is already running for this project, return
    immediately with ``{rebuilding: True}`` instead of starting a duplicate build
    (which would block the request and double-build). The graph will be fresh
    shortly; the agent can confirm via ``graph_status``. ``family`` builds the
    merged family graph (native global-graph mechanism) instead.
    """
    if family:
        return _build_family_graph(family, include_html=include_html, directed=directed)
    out_root = _resolve_root(workspace_path)
    if _rebuild_in_flight(workspace_path, root):
        return ok_obj(
            success=True,
            rebuilding=True,
            out_dir=str(_codegraph_dir(out_root)),
            note="graph rebuild already in progress — will be fresh shortly; no new build started",
        )
    return build_graph(workspace_path, root, include_html=include_html, directed=directed, project_id=project_id)


def _build_graph_impl(
    workspace_path: str | Path,
    root: str | Path | None = None,
    include_html: bool = True,
    directed: bool = False,
    project_id: str | None = None,
    out_dir: str | Path | None = None,
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

    Returns ``{success, out_dir, nodes, edges, files, built_at, incremental}``
    or an error dict.
    """
    g = _graphify_imports()
    if g is None:
        return fail_obj(error="graphifyy is not installed")

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

        rel_files = [str(f).replace("\\", "/") for f in files]
        prev_graph = _graph_from_json(out_dir) if prev_manifest else None

        # Fresh-skip: prior graph exists AND nothing changed → nothing to rebuild.
        # Makes no-op rebuilds (e.g. a graph_fresh precondition re-check) instant
        # instead of re-running a warm full-corpus extract.
        if prev_graph is not None and not changed:
            artifacts = {"graph.json": str(out_dir / "graph.json")}
            if include_html and (out_dir / "graph.html").is_file():
                artifacts["graph.html"] = str(out_dir / "graph.html")
            return ok_obj(
                out_dir=str(out_dir),
                artifacts=artifacts,
                nodes=len(prev_graph["nodes"]),
                edges=len(prev_graph["edges"]),
                files=len(rel_files),
                built_at=(prev_manifest or {}).get("built_at"),
                incremental=False,
                skipped=True,
            )

        # Incremental path: prior graph exists AND only some files changed.
        incremental = bool(prev_graph and changed and len(changed) < len(rel_files))
        if incremental:
            # ``incremental`` implies prev_graph is not None (Pylance cannot
            # narrow through the bool(...) wrapper — assert explicitly).
            assert prev_graph is not None
            changed_set = set(changed)
            # ``changed`` holds RELATIVE relpaths (from _source_manifest) while
            # ``files`` are ABSOLUTE — compare against each file's relpath.
            to_extract = [f for f in files if str(f.relative_to(scan_root)).replace("\\", "/") in changed_set]
            # Re-extract changed files; feed the unchanged corpus as resolution
            # context so cross-file edges (calls, method refs) still resolve.
            fresh = g["extract"](
                to_extract,
                root=scan_root,
                cache_root=cache_root,
                parallel=_use_parallel(),
                resolution_context_nodes=prev_graph["nodes"],
                resolution_context_edges=prev_graph["edges"],
            )
            extraction = _merge_extractions(prev_graph, fresh, changed)
        else:
            # Full build (first time, or nearly everything changed).
            extraction = g["extract"](files, root=scan_root, cache_root=cache_root, parallel=_use_parallel())

        graph = g["build_from_json"](extraction, root=scan_root, directed=directed)
        communities = g["cluster"](graph)

        graph_path = out_dir / "graph.json"
        # ``force``: on an incremental rebuild a smaller graph is legitimate
        # (a file was deleted, or symbols were removed from a changed file) —
        # graphify's shrink guard would otherwise refuse to overwrite and stale
        # nodes would persist. The merge is fully controlled (unchanged files
        # carried over from the prior graph), so the reduction is trusted here.
        # ``built_at_commit`` is passed explicitly so graphify skips its
        # subprocess ``git`` call, which deadlocks in the frozen exe (see
        # _git_head_safe).
        g["to_json"](
            graph,
            communities,
            str(graph_path),
            force=incremental,
            built_at_commit=_git_head_safe(out_root),
        )

        artifacts = {"graph.json": str(graph_path)}
        if include_html:
            html_path = out_dir / "graph.html"
            g["to_html"](graph, communities, str(html_path))
            artifacts["graph.html"] = str(html_path)

        built_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "built_at": built_at,
            "scan_root": str(scan_root),
            "output_root": str(out_root),
            "total_files": len(rel_files),
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "source_manifest": cur_manifest,
        }
        (out_dir / ".build_state.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _ensure_readme(out_dir)

        # Write-time feedback: record changed files in memory.
        _write_feedback(workspace_path, scan_root, prev_manifest, manifest, project_id)

        return ok_obj(
            out_dir=str(out_dir),
            artifacts=artifacts,
            nodes=graph.number_of_nodes(),
            edges=graph.number_of_edges(),
            files=len(rel_files),
            built_at=built_at,
            incremental=incremental,
            gitignore_fallback=gitignore_fallback,
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
        "- `cache/` — graphify's own extraction cache (regenerable; safe to delete)\n",
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
    fresh = not changed and not removed

    return ok_obj(
        exists=True,
        fresh=fresh,
        built_at=state.get("built_at"),
        nodes=state.get("nodes"),
        edges=state.get("edges"),
        total_files=len(cur),
        changed_files=changed[:20],
        removed_files=removed[:10],
    )


def ensure_fresh(
    workspace_path: str | Path,
    root: str | Path | None = None,
    *,
    background: bool = False,
    family: str | None = None,
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
            started = _background_rebuild(workspace_path, r)
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
