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

import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .response import fail_obj, ok_obj

# Directories never indexed as source (mirrors graphify's own noise exclusions).
_NOISE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ai",
    "logs",
    ".eggs",
    "*.egg-info",
}


def _source_manifest(root: Path) -> dict[str, str]:
    """Return {relpath: '<mtime_ns>:<size>'} for all source files under root (noise excluded).

    Uses ``st_mtime_ns`` (nanosecond) so two writes within the same wall-clock
    second are still detected as changed — whole-second ``st_mtime`` misses
    rapid edits, which silently skips incremental rebuilds.
    """
    manifest: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS and not (d.endswith(".egg-info"))]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
                rel = str(path.relative_to(root)).replace("\\", "/")
                manifest[rel] = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                continue
    return manifest


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
        from .agent_recall import add_observations, create_entities

        preview = changed[:10]
        note = f"graphify: {len(changed)} file(s) changed at {datetime.now(timezone.utc).isoformat()}: " + ", ".join(
            preview
        )
        create_entities(
            workspace_path=workspace_path,
            project_id=project_id,
            entities=[{"name": "graphify_feedback", "entityType": "concept", "observations": []}],
        )
        add_observations(
            workspace_path=workspace_path,
            project_id=project_id,
            observations=[{"entityName": "graphify_feedback", "contents": [note]}],
        )
    except Exception:  # noqa: BLE001 — best-effort, never break the build
        pass


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
    all_files: list[str],
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
    prev_placeholder_ids = {
        str(n.get("id") or "") for n in prev_nodes if not n.get("source_file")
    }
    fresh_nodes = [
        n
        for n in fresh_nodes
        if n.get("source_file") or str(n.get("id") or "") in prev_placeholder_ids
    ]

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
) -> dict[str, Any]:
    """Build the code knowledge graph into ``<root>/.ai/codegraph/`` (AST-only, no LLM).

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

    root = Path(root or workspace_path).resolve()
    out_dir = _codegraph_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Capture the previous manifest BEFORE overwriting (for feedback + diff).
    prev_manifest = _load_manifest(out_dir)
    cur_manifest = _source_manifest(root)
    changed = _changed_files(prev_manifest, cur_manifest)

    try:
        detected = g["detect"](root, cache_root=root)
        files = [Path(f) for lst in detected.get("files", {}).values() for f in lst]
        if not files:
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
            to_extract = [
                f for f in files if str(f.relative_to(root)).replace("\\", "/") in changed_set
            ]
            # Re-extract changed files; feed the unchanged corpus as resolution
            # context so cross-file edges (calls, method refs) still resolve.
            fresh = g["extract"](
                to_extract,
                root=root,
                cache_root=root,
                parallel=True,
                resolution_context_nodes=prev_graph["nodes"],
                resolution_context_edges=prev_graph["edges"],
            )
            extraction = _merge_extractions(prev_graph, fresh, changed, rel_files)
        else:
            # Full build (first time, or nearly everything changed).
            extraction = g["extract"](files, root=root, cache_root=root, parallel=True)

        graph = g["build_from_json"](extraction, root=root, directed=directed)
        communities = g["cluster"](graph)

        graph_path = out_dir / "graph.json"
        # ``force``: on an incremental rebuild a smaller graph is legitimate
        # (a file was deleted, or symbols were removed from a changed file) —
        # graphify's shrink guard would otherwise refuse to overwrite and stale
        # nodes would persist. The merge is fully controlled (unchanged files
        # carried over from the prior graph), so the reduction is trusted here.
        g["to_json"](graph, communities, str(graph_path), force=incremental)

        artifacts = {"graph.json": str(graph_path)}
        if include_html:
            html_path = out_dir / "graph.html"
            g["to_html"](graph, communities, str(html_path))
            artifacts["graph.html"] = str(html_path)

        built_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "built_at": built_at,
            "scan_root": str(root),
            "total_files": len(rel_files),
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "source_manifest": cur_manifest,
        }
        (out_dir / ".build_state.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _ensure_readme(out_dir)

        # Write-time feedback: record changed files in memory.
        _write_feedback(workspace_path, root, prev_manifest, manifest, project_id)

        return ok_obj(
            out_dir=str(out_dir),
            artifacts=artifacts,
            nodes=graph.number_of_nodes(),
            edges=graph.number_of_edges(),
            files=len(rel_files),
            built_at=built_at,
            incremental=incremental,
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


def graph_status(workspace_path: str | Path, root: str | Path | None = None) -> dict[str, Any]:
    """Return whether the graph exists and is fresh (source unchanged since last build)."""
    root = Path(root or workspace_path).resolve()
    out_dir = _codegraph_dir(root)
    state_path = out_dir / ".build_state.json"

    if not state_path.is_file() or not (out_dir / "graph.json").is_file():
        return ok_obj(exists=False, fresh=False, error="no graph built")

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ok_obj(exists=False, fresh=False, error="corrupt build state")

    prev = state.get("source_manifest", {})
    cur = _source_manifest(root)
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


def ensure_fresh(workspace_path: str | Path, root: str | Path | None = None) -> dict[str, Any]:
    """Idempotent freshness guarantee: rebuild only when missing or stale.

    Returns ``{success, fresh, updated}`` where ``updated`` is 1 if a rebuild ran.
    """
    st = graph_status(workspace_path, root)
    if st.get("fresh"):
        return ok_obj(fresh=True, updated=0, exists=st.get("exists"))
    result = build_graph(workspace_path, root)
    result["fresh"] = True
    result["updated"] = 1 if result.get("success") else 0
    return result


# ══════════════════════════════════════════════════════════════════════════
# ── Read queries (deterministic, over graph.json — no LLM) ────────────────
# ══════════════════════════════════════════════════════════════════════════


def _load_graph(workspace_path: str | Path, root: str | Path | None = None) -> dict[str, Any] | None:
    """Load the built graph.json, or None if it doesn't exist."""
    root = Path(root or workspace_path).resolve()
    graph_path = _codegraph_dir(root) / "graph.json"
    if not graph_path.is_file():
        return None
    try:
        return json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _find_node_id(data: dict[str, Any], term: str) -> str | None:
    """Find a node id by exact label match, else first substring label match."""
    t = (term or "").strip().lower()
    if not t:
        return None
    for n in data.get("nodes", []):
        if (n.get("label") or "").lower() == t:
            return n.get("id")
    for n in data.get("nodes", []):
        if t in (n.get("label") or "").lower():
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


def query_graph(
    workspace_path: str | Path,
    query: str,
    limit: int = 10,
    root: str | Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Search graph nodes by label / source file / type (case-insensitive, ranked).

    Appends ``related_memory`` (memory entities matching the query term / source
    files) so the agent sees code + memory together (read-time correlation).
    """
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
        }
        for _, n in scored[: max(1, int(limit or 10))]
    ]
    return ok_obj(
        count=len(scored),
        results=results,
        related_memory=_related_memory(workspace_path, q, project_id=project_id, limit=5),
    )


def path_query(
    workspace_path: str | Path,
    a: str,
    b: str,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Shortest path (BFS) between two nodes by label."""
    data = _load_graph(workspace_path, root)
    if data is None:
        return fail_obj(error="no graph built (run graph_build)")

    a_id, b_id = _find_node_id(data, a), _find_node_id(data, b)
    if a_id is None or b_id is None:
        missing = [t for t, i in ((a, a_id), (b, b_id)) if i is None]
        return fail_obj(error=f"node(s) not found: {', '.join(missing)}")

    adj: dict[str, list[str]] = {}
    for link in data.get("links", []):
        s, tgt = link.get("source"), link.get("target")
        adj.setdefault(s, []).append(tgt)
        adj.setdefault(tgt, []).append(s)

    frontier = deque([a_id])
    prev: dict[str, str | None] = {a_id: None}
    while frontier:
        cur = frontier.popleft()
        if cur == b_id:
            break
        for nb in adj.get(cur, []):
            if nb not in prev:
                prev[nb] = cur
                frontier.append(nb)

    if b_id not in prev:
        return fail_obj(error="no path found between the two nodes")

    ids: list[str] = []
    cur: str | None = b_id
    while cur is not None:
        ids.append(cur)
        cur = prev[cur]
    ids.reverse()

    label = {n.get("id"): n.get("label") for n in data.get("nodes", [])}
    return ok_obj(
        path=[label.get(i, i) for i in ids],
        hops=max(0, len(ids) - 1),
    )


def explain_node(
    workspace_path: str | Path,
    node: str,
    limit: int = 30,
    root: str | Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Explain a node: its details + direct neighbours with relation types.

    Appends ``related_memory`` (memory entities matching the symbol label and/or
    its source file) so the agent sees code + memory together.
    """
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
    )
