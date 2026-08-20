"""
Background worker hardening (plan 183eba8c Phase 2.2-2.5):

1. Bug 3 — `graph_build(background:false)` proceeds synchronously even while a
   background worker is in flight (it serializes on the bounded build lock)
   instead of returning "rebuild already in progress".
2. Stall-aware `_rebuild_in_flight` — a worker that has not finished a chunk
   within the stall window is treated as stuck (not "in flight"), so subsequent
   builds are not blocked forever by a zombie thread.
3. `force=True` bypasses the in-flight guard.
4. Chunk-drain loop semantics: drains while `remaining_files > 0`, stops on
   `0`/`None`, and breaks (with a real error) when progress stalls.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from mcp_server.helpers import graphify_bridge as gb
from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def _action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    if action == "graph_build":
        params = dict(params or {})
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _start_fake_worker(ws: str, progress_fresh: bool = True) -> threading.Thread:
    """Inject a fake 'in-flight' worker thread (alive, optionally fresh progress)."""
    t = threading.Thread(target=lambda: time.sleep(10_000), name="fake-worker", daemon=True)
    t.start()
    key = str(Path(ws).resolve())
    gb._BACKGROUND_THREADS[key] = t
    gb._BACKGROUND_PROGRESS[key] = time.monotonic() if progress_fresh else time.monotonic() - 10_000
    return t


def _cleanup(ws: str) -> None:
    key = str(Path(ws).resolve())
    gb._BACKGROUND_THREADS.pop(key, None)
    gb._BACKGROUND_PROGRESS.pop(key, None)
    gb._BACKGROUND_ERRORS.pop(key, None)


@pytest.fixture(autouse=True)
def _isolate():
    """Never leak fake workers across tests."""
    yield
    gb._BACKGROUND_THREADS.clear()
    gb._BACKGROUND_PROGRESS.clear()
    gb._BACKGROUND_ERRORS.clear()


# ── Bug 3: background:false proceeds while in flight ────────────────────────


def test_background_false_proceeds_while_inflight(tmp_path: Path):
    """background:false must NOT return 'rebuilding' — it processes a chunk."""
    for i in range(20):
        (tmp_path / f"m{i:02}.py").write_text(f"def m{i:02}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)
    _start_fake_worker(ws, progress_fresh=True)

    try:
        r = gb.graph_build_action(workspace_path=ws, background=False, chunk_size=5, include_html=False)
        assert r.get("rebuilding") is not True, r
        assert r.get("success") is True
        assert r.get("processed_files") == 5  # one synchronous chunk processed
    finally:
        _cleanup(ws)


# ── Stall-aware _rebuild_in_flight ─────────────────────────────────────────


def test_stalled_worker_not_inflight(tmp_path: Path):
    """A worker alive but stalled (old progress) no longer counts as in-flight."""
    ws = str(tmp_path)
    _start_fake_worker(ws, progress_fresh=False)
    try:
        assert gb._rebuild_in_flight(ws) is False
    finally:
        _cleanup(ws)


def test_healthy_worker_is_inflight(tmp_path: Path):
    ws = str(tmp_path)
    _start_fake_worker(ws, progress_fresh=True)
    try:
        assert gb._rebuild_in_flight(ws) is True
    finally:
        _cleanup(ws)


# ── force bypasses the guard ───────────────────────────────────────────────


def test_force_bypasses_guard(tmp_path: Path):
    """force=True skips the in-flight guard for the background trigger."""
    for i in range(8):
        (tmp_path / f"f{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)
    _start_fake_worker(ws, progress_fresh=True)
    try:
        r = gb.graph_build_action(workspace_path=ws, background=True, force=True, chunk_size=5, include_html=False)
        assert r.get("rebuilding") is not True, r
    finally:
        _cleanup(ws)


# ── Chunk-drain loop semantics (unit, via a stubbed build_graph) ───────────


def test_chunk_worker_drains_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = str(tmp_path)
    seq = [
        {"success": True, "remaining_files": 30},
        {"success": True, "remaining_files": 20},
        {"success": True, "remaining_files": 10},
        {"success": True, "remaining_files": 0},
    ]
    calls: list[dict] = []

    def fake_build_graph(*args, **kwargs):
        calls.append(kwargs)
        return seq[min(len(calls) - 1, len(seq) - 1)]

    monkeypatch.setattr(gb, "build_graph", fake_build_graph)
    assert gb._background_rebuild(ws, Path(ws).resolve(), chunk_size=10) is True

    deadline = time.time() + 10
    while time.time() < deadline and gb._rebuild_in_flight(ws):
        time.sleep(0.02)
    assert not gb._rebuild_in_flight(ws)
    assert len(calls) == 4  # drained 30 → 20 → 10 → 0
    assert gb._BACKGROUND_ERRORS.get(str(Path(ws).resolve())) is None


def test_chunk_worker_stops_on_none_remaining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """remaining_files None (not chunked/unknown) stops the loop rather than spinning."""
    ws = str(tmp_path)
    calls: list[dict] = []

    def fake_build_graph(*args, **kwargs):
        calls.append(kwargs)
        return {"success": True, "remaining_files": None}

    monkeypatch.setattr(gb, "build_graph", fake_build_graph)
    assert gb._background_rebuild(ws, Path(ws).resolve(), chunk_size=10) is True

    deadline = time.time() + 10
    while time.time() < deadline and gb._rebuild_in_flight(ws):
        time.sleep(0.02)
    assert not gb._rebuild_in_flight(ws)
    assert len(calls) == 1


def test_chunk_worker_breaks_on_no_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """remaining_files stuck (did not decrease) → break with a real background_error."""
    ws = str(tmp_path)
    calls: list[dict] = []

    def fake_build_graph(*args, **kwargs):
        calls.append(kwargs)
        return {"success": True, "remaining_files": 30}  # never decreases

    monkeypatch.setattr(gb, "build_graph", fake_build_graph)
    assert gb._background_rebuild(ws, Path(ws).resolve(), chunk_size=10) is True

    deadline = time.time() + 10
    while time.time() < deadline and gb._rebuild_in_flight(ws):
        time.sleep(0.02)
    assert not gb._rebuild_in_flight(ws)
    assert len(calls) == 2  # first chunk, then the no-progress break
    err = gb._BACKGROUND_ERRORS.get(str(Path(ws).resolve()))
    assert err and "did not decrease" in err


def test_chunk_worker_surfaces_build_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A failed chunk surfaces a real background_error."""
    ws = str(tmp_path)

    def fake_build_graph(*args, **kwargs):
        return {"success": False, "error": "boom"}

    monkeypatch.setattr(gb, "build_graph", fake_build_graph)
    assert gb._background_rebuild(ws, Path(ws).resolve(), chunk_size=10) is True

    deadline = time.time() + 10
    while time.time() < deadline and gb._rebuild_in_flight(ws):
        time.sleep(0.02)
    assert not gb._rebuild_in_flight(ws)
    err = gb._BACKGROUND_ERRORS.get(str(Path(ws).resolve()))
    assert err and "boom" in err


# ── Phase 3: persisted worker lifecycle + cumulative graph_status ──────────


def _write_state(tmp_path: Path, **overrides) -> Path:
    """Write a minimal .build_state.json + graph.json into tmp_path/.ai/codegraph."""
    out = tmp_path / ".ai" / "codegraph"
    out.mkdir(parents=True, exist_ok=True)
    state = {
        "built_at": "2026-01-01T00:00:00+00:00",
        "scan_root": str(tmp_path),
        "output_root": str(tmp_path),
        "total_files": 800,
        "nodes": 10,
        "edges": 5,
        "processed_files": 200,
        "remaining_files": 600,
        "source_manifest": {"a.py": "1:1", "b.py": "2:2"},
    }
    state.update(overrides)
    state_path = out / ".build_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (out / "graph.json").write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    return state_path


def test_graph_status_reports_cumulative_processed(tmp_path: Path):
    """graph_status.processed_files = total - remaining (cumulative), not last chunk."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _write_state(tmp_path)
    st = gb.graph_status(str(tmp_path))
    assert st["processed_files"] == 200  # 800 total - 600 remaining
    assert st["processed_total"] == 200
    assert st["processed_this_chunk"] == 200
    assert st["remaining_files"] == 600
    # Advance one chunk: remaining 400 → cumulative 400 (per-run stays 200).
    state_path = tmp_path / ".ai" / "codegraph" / ".build_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["remaining_files"] = 400
    state_path.write_text(json.dumps(state), encoding="utf-8")
    st2 = gb.graph_status(str(tmp_path))
    assert st2["processed_files"] == 400
    assert st2["processed_this_chunk"] == 200


def test_stale_persisted_rebuilding_autoclears(tmp_path: Path):
    """rebuilding:true with no live worker → status reports false + heals manifest."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    state_path = _write_state(tmp_path, rebuilding=True, rebuilding_started_at="2026-01-01T00:00:00+00:00")
    ws = str(tmp_path)
    assert gb._rebuild_in_flight(ws) is False  # no live worker
    st = gb.graph_status(ws)
    assert st["rebuilding"] is False
    healed = json.loads(state_path.read_text(encoding="utf-8"))
    assert healed.get("rebuilding") is False


def test_live_worker_reports_rebuilding(tmp_path: Path):
    """A live (fresh-progress) worker reports rebuilding:true in graph_status."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _write_state(tmp_path)
    ws = str(tmp_path)
    _start_fake_worker(ws, progress_fresh=True)
    try:
        st = gb.graph_status(ws)
        assert st["rebuilding"] is True
    finally:
        _cleanup(ws)


def test_background_error_persisted_and_surfaced(tmp_path: Path):
    """_bg_error persists rebuilding_error into the manifest; status surfaces it."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _write_state(tmp_path)
    ws = str(tmp_path)
    key = str(Path(ws).resolve())
    gb._bg_error(key, "worker boom")
    try:
        st = gb.graph_status(ws)
        assert st["background_error"] == "worker boom"
    finally:
        gb._BACKGROUND_ERRORS.pop(key, None)
    # Survives the in-memory error being gone (restart scenario → from manifest).
    st2 = gb.graph_status(ws)
    assert st2["background_error"] == "worker boom"


def test_worker_persists_lifecycle_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A running worker writes rebuilding:true; finishing clears it to false."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    state_path = _write_state(tmp_path)
    ws = str(tmp_path)
    seq = [{"success": True, "remaining_files": 10}, {"success": True, "remaining_files": 0}]
    calls: list[dict] = []

    def fake_build_graph(*args, **kwargs):
        calls.append(kwargs)
        return seq[min(len(calls) - 1, len(seq) - 1)]

    monkeypatch.setattr(gb, "build_graph", fake_build_graph)
    gb._background_rebuild(ws, Path(ws).resolve(), chunk_size=5)
    # Immediately after start, the manifest carries the live marker.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("rebuilding") is True
    assert state.get("rebuilding_started_at")
    # Drain finishes → the flag is cleared.
    deadline = time.time() + 10
    while time.time() < deadline and gb._rebuild_in_flight(ws):
        time.sleep(0.02)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("rebuilding") is False


# ── Phase 4: exclusion visibility + force wiring ───────────────────────────


def test_graph_status_reports_exclusion_counts(tmp_path: Path):
    """graph_status reports scanned vs .graphignore-excluded vs supported counts."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (tmp_path / "skip.py").write_text("def s():\n    return 3\n", encoding="utf-8")
    (tmp_path / ".graphignore").write_text("skip.py\n", encoding="utf-8")
    ws = str(tmp_path)
    r = gb.build_graph(ws, chunk_size=0, include_html=False)
    assert r["success"] is True
    st = gb.graph_status(ws)
    assert st["supported_files"] == 2  # a.py + b.py (skip.py excluded by .graphignore)
    assert st["scanned_files"] == 3  # all three before project exclusion rules
    assert st["excluded_files"] == 1


async def test_graph_build_force_param_wired(tmp_path: Path):
    """graph_build action accepts `force` (wired through the registry spec)."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)
    r = await _action_call("graph_build", {"workspace_path": ws, "force": True, "include_html": False})
    assert r["success"] is True, r
    assert r["result"].get("processed_files") == 1
    assert r["result"].get("remaining_files") == 0


# ── Phase 5: partial-build status truth ────────────────────────────────────


def test_graph_status_exists_true_during_partial_build(tmp_path: Path):
    """A partial chunked build (manifest + graph.json on disk, remaining > 0)
    reports exists:true (not the stale 'exists:false' the bug report showed)."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    _write_state(tmp_path)  # total 800, remaining 600, graph.json present
    st = gb.graph_status(str(tmp_path))
    assert st["exists"] is True
    assert st["fresh"] is False
    assert st["remaining_files"] == 600
    assert st["processed_files"] == 200  # cumulative (800 - 600)


async def test_graph_status_exists_during_partial_build(tmp_path: Path):
    """graph_status reports exists:true (partial graph on disk) while incomplete."""
    for i in range(10):
        (tmp_path / f"m{i:02}.py").write_text(f"def m{i:02}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)
    r = await _action_call("graph_build", {"workspace_path": ws, "max_files": 2, "include_html": False})
    assert r["success"] is True, r
    assert r["result"]["partial"] is True
    st = await _action_call("graph_status", {"workspace_path": ws})
    assert st["result"]["exists"] is True  # partial graph.json is on disk
    assert st["result"]["fresh"] is False  # still incomplete
    assert st["result"]["remaining_files"] > 0
