from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers.agent_recall import family_members
from ...helpers.response import fail_obj, ok_obj
from .build import _background_rebuild, _rebuild_in_flight, build_graph
from .config import _BACKGROUND_THRESHOLD, _codegraph_dir, _resolve_root
from .exclusions import _changed_files, _gitignore_exclusions, _scanned_count, _source_manifest
from .family import _build_family_graph, _family_status
from .manifest import _BACKGROUND_ERRORS, _load_manifest, _manifest_update


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
