# ruff: noqa: E501
from __future__ import annotations

import gc
import hashlib
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers.embeddings import add_to_index
from ...helpers.response import fail_obj, ok_obj
from .alias import (
    _augment_alias_import_edges,
    _augment_alias_import_edges_json,
)
from .config import (
    _BACKGROUND_PROGRESS,
    _BACKGROUND_STALL_SECONDS,
    _BACKGROUND_THREADS,
    _BUILD_LOCK_TIMEOUT,
    _BUILD_LOCKS_GUARD,
    _ENABLE_PHP_IMPLEMENTS_ENRICHMENT,
    _REBUILD_STATE_KEYS,
    _WATCHDOG_THREADS,
    _build_lock,
    _codegraph_dir,
    _resolve_root,
    _use_parallel,
)
from .enrichment import _clean_inconsistencies, _enrich_laravel, _enrich_php_implements, _enrich_vue
from .exclusions import (
    _GLOBAL_EXCLUSIONS,
    _changed_files,
    _gitignore_exclusions,
    _gitignored,
    _is_noise_relpath,
    _source_manifest,
)
from .html import _graphify_imports, _html_export
from .io import _graph_from_json
from .manifest import _BACKGROUND_ERRORS, _bg_error, _load_manifest, _manifest_update, _mark_progress


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

                        # Yield CPU and force-clear AST parsing memory leak
                        time.sleep(0.05)
                        gc.collect()

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
            except Exception as exc:
                import traceback

                traceback.print_exc()
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
            try:
                while True:
                    time.sleep(5)
                    with _BUILD_LOCKS_GUARD:
                        current = _BACKGROUND_THREADS.get(key)
                        if current is not t or not t.is_alive():
                            return  # worker finished or replaced
                        last = _BACKGROUND_PROGRESS.get(key)
                        # Only flag stalled once the worker has reported at least
                        # one chunk (last is not None). Cold-start: the worker may be
                        # mid-extract and hasn't stamped progress yet — that's not
                        # a stall, it's a slow first chunk.
                        stalled = last is not None and (time.monotonic() - last) > _BACKGROUND_STALL_SECONDS
                    if stalled and key not in _BACKGROUND_ERRORS:
                        _bg_error(
                            key,
                            "background worker stalled: no chunk completed within "
                            f"{_BACKGROUND_STALL_SECONDS:.0f}s — the build lock may be stuck; "
                            "restart the server if it never drains",
                        )
            finally:
                with _BUILD_LOCKS_GUARD:
                    _WATCHDOG_THREADS.pop(key, None)

        wt = threading.Thread(target=_watchdog, name=f"graph-watchdog-{key[-16:]}", daemon=True)
        _WATCHDOG_THREADS[key] = wt
        wt.start()
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
        from ...helpers.agent_recall import add_observations, ensure_entities

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
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
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
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return ""


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

    from .family import _build_family_graph
    from .status import graph_status

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
    is_default_out = out_dir is None
    out_dir = _codegraph_dir(out_root) if out_dir is None else Path(out_dir)
    # Cache follows the OUTPUT location: default → <workspace_root>/.ai/codegraph/
    # (never the scan sub-root, so root="src" cannot drop .ai inside src/); under a
    # sandbox → <out_dir>/cache so NOTHING is written into the scanned project.
    cache_root = out_root if is_default_out else out_dir / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Backward compatibility: move incorrectly nested cache from older builds
    _correct_cache = cache_root / ".ai" / "codegraph" / "cache"
    _wrong_cache = _correct_cache / ".ai" / "codegraph" / "cache"
    if _wrong_cache.exists() and _wrong_cache.is_dir():
        import shutil

        _correct_cache.mkdir(parents=True, exist_ok=True)
        for item in _wrong_cache.iterdir():
            target = _correct_cache / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
        try:
            shutil.rmtree(_correct_cache / ".ai")
        except Exception:
            pass

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
            try:
                rel = path.relative_to(scan_root)
            except ValueError:
                return False  # outside scan root — not a source file
            rel_str = str(rel).replace("\\", "/")
            parent = "/".join(rel_str.split("/")[:-1])
            if _GLOBAL_EXCLUSIONS.excludes_file(parent, path.name):
                return False
            return not _is_noise_relpath(rel_str)

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
        import contextlib
        import sys

        with contextlib.redirect_stdout(sys.stderr):
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
        _clean_inconsistencies(graph)
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
        # Enrich nodes with semantic type and external flag
        for n, data in graph.nodes(data=True):
            fpath = str(data.get("source_file", data.get("filepath", "")))
            if not fpath:
                data["external"] = True

            if not data.get("type"):
                label = data.get("label", data.get("name", str(n)))
                if data.get("file_type") == "document":
                    data["type"] = "document"
                elif label.endswith("()"):
                    data["type"] = "function"
                elif label.endswith(".vue"):
                    data["type"] = "component"
                elif "." in label and label.split(".")[-1] in ("php", "js", "ts", "py"):
                    data["type"] = "file"
                elif label and label[0].isupper():
                    data["type"] = "class"
                else:
                    data["type"] = "symbol"
        _enrich_vue(graph, scan_root)
        _enrich_laravel(graph, scan_root)
        if _ENABLE_PHP_IMPLEMENTS_ENRICHMENT:
            _enrich_php_implements(graph, scan_root)

        g["to_json"](
            graph,
            communities,
            str(graph_path),
            force=bool(changed or incremental or chunked),
            built_at_commit=_git_head_safe(out_root),
        )

        if graph_path.exists():
            try:
                gdata = json.loads(graph_path.read_text("utf-8"))
                if "hyperedges" in gdata and not gdata["hyperedges"]:
                    del gdata["hyperedges"]
                    if "graph" in gdata and "hyperedges" in gdata["graph"]:
                        del gdata["graph"]["hyperedges"]
                    graph_path.write_text(json.dumps(gdata, ensure_ascii=False, indent=2), "utf-8")
            except Exception:
                pass

        # Embed nodes into LanceDB
        docs = []
        doc_ids = []
        for n, data in graph.nodes(data=True):
            nid = str(n)
            name = str(data.get("label", data.get("name", nid)))
            ntype = str(data.get("type", "unknown"))
            fpath = str(data.get("source_file", data.get("filepath", "")))
            content = f"Name: {name}\nType: {ntype}\nFile: {fpath}"
            docs.append(content)
            doc_ids.append(nid)

        if docs:
            pid = project_id or settings.get_project_id(workspace_path)
            if not pid:
                pid = hashlib.md5(str(workspace_path).encode("utf-8")).hexdigest()
            table_name = f"codegraph_{pid}"
            add_to_index(table_name, docs, doc_ids, workspace_path=workspace_path)

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
            "processed_files": max(0, total_files - remaining_files),
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

        import gc

        gc.collect()

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
    except Exception as e:
        traceback.print_exc()
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
