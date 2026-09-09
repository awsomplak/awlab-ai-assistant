"""Phase 4 — graceful shutdown: background graph rebuilds are cancellable.

The worker requests shutdown on SIGINT/SIGTERM, parent-bridge death, and stdio
EOF. The background chunk-drain loop must stop between chunks so a busy rebuild
does not grind to completion while the worker is being torn down.
"""

import time

from mcp_server.modules.graphify import build as build_mod
from mcp_server.modules.graphify.config import request_shutdown, shutdown_requested


def _reset() -> None:
    from mcp_server.modules.graphify import config as cfg

    cfg._SHUTDOWN_EVENT.clear()


def _thread_gone(key: str, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = build_mod._BACKGROUND_THREADS.get(key)
        if t is None or not t.is_alive():
            return True
        time.sleep(0.05)
    return False


def test_shutdown_event_lifecycle():
    _reset()
    assert shutdown_requested() is False
    request_shutdown()
    assert shutdown_requested() is True
    _reset()
    assert shutdown_requested() is False


def test_chunk_loop_does_not_start_when_shutdown_already_requested(monkeypatch, tmp_path):
    _reset()
    calls: list = []
    monkeypatch.setattr(
        build_mod,
        "build_graph",
        lambda *a, **k: calls.append(1) or {"success": True, "remaining_files": 5},
    )
    (tmp_path / ".ai" / "codegraph").mkdir(parents=True, exist_ok=True)
    request_shutdown()  # requested before the worker starts
    started = build_mod._background_rebuild(str(tmp_path), tmp_path, chunk_size=1)
    assert started is True
    key = str(tmp_path.resolve())
    assert _thread_gone(key), "chunk worker did not exit when shutdown was already requested"
    assert calls == [], "chunk worker ran a build despite shutdown being requested"


def test_chunk_loop_stops_between_chunks_on_shutdown(monkeypatch, tmp_path):
    _reset()
    calls: list = []
    counter = {"n": 10_000}

    def fake_build(*_a, **_k):
        time.sleep(0.02)  # simulate real per-chunk work
        calls.append(1)
        counter["n"] -= 1
        return {"success": True, "remaining_files": counter["n"]}

    monkeypatch.setattr(build_mod, "build_graph", fake_build)
    (tmp_path / ".ai" / "codegraph").mkdir(parents=True, exist_ok=True)
    started = build_mod._background_rebuild(str(tmp_path), tmp_path, chunk_size=1)
    assert started is True

    # Let a few chunks run, then request shutdown mid-drain.
    deadline = time.time() + 10
    while time.time() < deadline and len(calls) < 3:
        time.sleep(0.02)
    assert len(calls) >= 1, "chunk worker never ran a chunk"
    before = len(calls)
    request_shutdown()
    key = str(tmp_path.resolve())
    assert _thread_gone(key, timeout=8), "chunk worker did not stop after shutdown was requested"
    # The worker must not have kept draining many more chunks after shutdown.
    assert len(calls) - before < 20, f"worker kept draining after shutdown ({len(calls) - before} more chunks)"
