"""
Graph build scalability + exclusions:
- Bounded builds: ``max_files`` (first build) + ``chunk_size`` (every run,
  Laravel-queue semantics) keep peak RAM/CPU flat on large projects.
- Graceful HTML viz limit (``node_limit``, default 20000 via config).
- ``.graphignore`` excludes files/dirs from the code graph without touching
  the project's .gitignore.
"""

import json
import time
from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    if action == "graph_build":
        params = dict(params or {})
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _reset(*keys: str) -> None:
    """Drop cached settings properties so a monkeypatched env is re-read."""
    for k in keys:
        settings.__dict__.pop(k, None)


# ── Config defaults & env overrides ───────────────────────────────────────


def test_graph_config_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT", raising=False)
    monkeypatch.delenv("GRAPH_MAX_FILES", raising=False)
    _reset("graph_viz_limit", "graph_max_files")
    assert settings.graph_viz_limit == 20000
    assert settings.graph_max_files is None


def test_graph_config_env_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GRAPHIFY_VIZ_NODE_LIMIT", "30000")
    monkeypatch.setenv("GRAPH_MAX_FILES", "25")
    _reset("graph_viz_limit", "graph_max_files")
    assert settings.graph_viz_limit == 30000
    assert settings.graph_max_files == 25


# ── Bounded builds (max_files + chunk_size queue semantics) ───────────────


async def test_graph_build_partial_then_chunked_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """max_files bounds the FIRST build; then chunk_size bounds EVERY subsequent
    run — repeated calls drain remaining_files to 0 and the graph goes fresh."""
    monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT", raising=False)
    _reset("graph_viz_limit")
    for i in range(8):
        (tmp_path / f"mod{i}.py").write_text(f"def func{i}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)

    # 1. max_files caps the first build → partial + pending.
    r1 = await action_call("graph_build", {"workspace_path": ws, "max_files": 2, "background": False})
    assert r1["success"] is True, r1
    assert r1["result"]["partial"] is True
    assert r1["result"]["pending_files"] >= 6
    assert r1["result"]["files"] == 2
    st = await action_call("graph_status", {"workspace_path": ws})
    assert st["result"]["fresh"] is False

    # 2. chunk_size bounds every run — two more calls finish it (3 then 3).
    r2 = await action_call("graph_build", {"workspace_path": ws, "chunk_size": 3, "background": False})
    assert r2["success"] is True, r2
    assert r2["result"]["chunked"] is True
    assert r2["result"]["processed_files"] == 3
    assert r2["result"]["remaining_files"] == 3

    r3 = await action_call("graph_build", {"workspace_path": ws, "chunk_size": 3, "background": False})
    assert r3["success"] is True, r3
    assert r3["result"]["chunked"] is False
    assert r3["result"]["processed_files"] == 3
    assert r3["result"]["remaining_files"] == 0

    st2 = await action_call("graph_status", {"workspace_path": ws})
    assert st2["result"]["fresh"] is True
    assert st2["result"]["remaining_files"] == 0


# ── Graceful HTML viz limit ────────────────────────────────────────────────


async def test_graph_build_html_default_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Small graph under the default 20000 limit → full HTML render."""
    monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT", raising=False)
    _reset("graph_viz_limit")
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)
    r = await action_call("graph_build", {"workspace_path": ws})
    assert r["success"] is True
    assert r["result"]["html"] == "full"
    assert r["result"]["node_limit"] == 20000
    assert (tmp_path / ".ai" / "codegraph" / "graph.html").is_file()


async def test_graph_build_node_limit_graceful_and_zero(tmp_path: Path):
    """node_limit over-limit → aggregated community view (never raises); 0 →
    skips HTML entirely. Structural graph.json + manifest always land."""
    for i in range(4):
        (tmp_path / f"f{i}.py").write_text(f"def func{i}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)

    r = await action_call("graph_build", {"workspace_path": ws, "node_limit": 1})
    assert r["success"] is True, r
    # aggregated (multi-community) or the single-community early-return — never
    # raises, and the structural artifacts always land.
    assert r["result"]["html"] in ("aggregated", None)
    assert r["result"]["node_limit"] == 1
    assert (tmp_path / ".ai" / "codegraph" / "graph.json").is_file()
    assert (tmp_path / ".ai" / "codegraph" / ".build_state.json").is_file()

    r0 = await action_call("graph_build", {"workspace_path": ws, "node_limit": 0})
    assert r0["success"] is True
    assert r0["result"]["html"] is None
    assert "graph.html" not in r0["result"]["artifacts"]


# ── Background queue worker (chunk completion) ─────────────────────────────


def test_graph_build_chunk_size_background_completes(tmp_path: Path):
    """A normal graph_build triggers a queue worker without processing a chunk
    synchronously, then the worker advances until the graph is complete."""
    from mcp_server.helpers import graphify_bridge as gb

    proj = tmp_path / "chunkbg"
    proj.mkdir()
    n = 40
    for i in range(n):
        (proj / f"b{i:02d}.py").write_text(f"def b{i:02d}():\n    return {i}\n", encoding="utf-8")
    ws = str(proj)

    r = gb.graph_build_action(ws, chunk_size=10)
    assert r.get("success") is True, r
    assert r.get("triggered") is True
    assert r.get("background") is True
    assert r.get("background_started") is True
    assert gb._rebuild_in_flight(ws) is True

    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline and not gb.graph_status(ws).get("fresh"):
        time.sleep(0.2)
    st = gb.graph_status(ws)
    assert st.get("fresh") is True
    assert st.get("remaining_files", 0) == 0


def test_graph_build_trigger_does_not_block_on_first_chunk(tmp_path: Path):
    """The background trigger returns progress metadata immediately; work is
    performed by the worker, not inside the graph_build request."""
    from mcp_server.helpers import graphify_bridge as gb

    for i in range(20):
        (tmp_path / f"trigger{i:02d}.py").write_text(f"def trigger{i:02d}():\n    return {i}\n", encoding="utf-8")
    result = gb.graph_build_action(str(tmp_path), chunk_size=5, include_html=False)
    assert result["success"] is True
    assert result["triggered"] is True
    assert "nodes" not in result
    assert gb._rebuild_in_flight(str(tmp_path)) is True

    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline and not gb.graph_status(str(tmp_path)).get("fresh"):
        time.sleep(0.1)
    assert gb.graph_status(str(tmp_path)).get("fresh") is True


def test_graph_reads_pending_during_incomplete_build(tmp_path: Path):
    """Queries do not return partial nodes while a chunked build is incomplete."""
    from mcp_server.helpers import graphify_bridge as gb

    for i in range(12):
        (tmp_path / f"pending{i:02d}.py").write_text(f"def pending{i:02d}():\n    return {i}\n", encoding="utf-8")
    workspace = str(tmp_path)
    first = gb.build_graph(workspace, chunk_size=3, include_html=False)
    assert first["success"] is True
    assert first["remaining_files"] > 0

    pending = gb.query_graph(workspace, "pending11")
    assert pending["mode"] == "pending"
    assert pending["graph_pending"] is True
    assert pending["results"] == []


async def test_graph_build_chunked_first_build_overwrites_existing_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A chunked first build (prev manifest missing/corrupt -> prev_graph=None)
    must OVERWRITE an existing larger graph.json — the shrink guard must not
    leave it stale while .build_state.json advances (regression: graph.json vs
    build_state mismatch, worker appearing stuck at chunk 1)."""
    monkeypatch.delenv("GRAPH_MAX_FILES", raising=False)
    monkeypatch.delenv("GRAPH_CHUNK_SIZE", raising=False)
    monkeypatch.delenv("GRAPHIFY_VIZ_NODE_LIMIT", raising=False)
    _reset("graph_max_files", "graph_chunk_size", "graph_viz_limit")
    from mcp_server.helpers import graphify_bridge as gb

    for i in range(30):
        (tmp_path / f"m{i:03d}.py").write_text(f"def f{i:03d}():\n    return {i}\n", encoding="utf-8")
    ws = str(tmp_path)

    # Existing full graph.
    full = gb.build_graph(ws, chunk_size=0, include_html=False)
    assert full["success"] is True and full["remaining_files"] == 0

    # Simulate a missing/unreadable old manifest -> prev_graph=None (first-build chunked).
    (tmp_path / ".ai" / "codegraph" / ".build_state.json").unlink()

    c1 = gb.build_graph(ws, chunk_size=5, include_html=False)
    assert c1["success"] is True, c1
    assert c1["chunked"] is True
    # graph.json MUST reflect the partial chunk, not the stale full graph.
    gj = json.loads((tmp_path / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))
    assert len(gj["nodes"]) == c1["nodes"]
    assert c1["nodes"] < full["nodes"]  # partial chunk is smaller than the full graph

    # Subsequent chunks advance consistently (graph.json grows with build_state).
    c2 = gb.build_graph(ws, chunk_size=5, include_html=False)
    assert c2["success"] is True
    assert c2["remaining_files"] < c1["remaining_files"]
    gj2 = json.loads((tmp_path / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))
    assert len(gj2["nodes"]) == c2["nodes"]


def test_graph_status_resumes_pending_empty_manifest(tmp_path: Path):
    """A partial state with an empty source_manifest is stale, not fresh, and
    the next normal chunk resumes instead of returning skipped=True forever."""
    from mcp_server.helpers import graphify_bridge as gb

    for i in range(12):
        (tmp_path / f"resume{i:02d}.py").write_text(f"def resume{i:02d}():\n    return {i}\n", encoding="utf-8")
    workspace = str(tmp_path)

    full = gb.build_graph(workspace, chunk_size=0, include_html=False)
    assert full["success"] is True
    state_path = tmp_path / ".ai" / "codegraph" / ".build_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["source_manifest"] = {}
    state["processed_files"] = 3
    state["remaining_files"] = 9
    state["chunked"] = True
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    status = gb.graph_status(workspace)
    assert status["fresh"] is False
    assert status["remaining_files"] == 9
    assert status["total_files"] == 12

    resumed = gb.build_graph(workspace, chunk_size=3, include_html=False)
    assert resumed["success"] is True, resumed
    assert resumed.get("skipped") is not True
    assert resumed["remaining_files"] < 9
    assert resumed["processed_files"] == 3


# ── .graphignore exclusion (gitignore-style, without .gitignore) ──────────


async def test_graph_build_graphignore_excludes_files(tmp_path: Path):
    """.graphignore excludes files/dirs from the code graph WITHOUT touching
    .gitignore — excluded symbols are not queryable."""
    (tmp_path / "keep.py").write_text("def keep_me():\n    return 1\n", encoding="utf-8")
    (tmp_path / "skip.py").write_text("def skip_me():\n    return 2\n", encoding="utf-8")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "gen.py").write_text("def gen_me():\n    return 3\n", encoding="utf-8")
    (tmp_path / ".graphignore").write_text("skip.py\ngenerated/\n", encoding="utf-8")
    ws = str(tmp_path)

    r = await action_call("graph_build", {"workspace_path": ws})
    assert r["success"] is True, r

    q_keep = await action_call("graph_query", {"workspace_path": ws, "query": "keep_me"})
    assert q_keep["result"]["count"] >= 1
    q_skip = await action_call("graph_query", {"workspace_path": ws, "query": "skip_me"})
    assert q_skip["result"]["count"] == 0
    q_gen = await action_call("graph_query", {"workspace_path": ws, "query": "gen_me"})
    assert q_gen["result"]["count"] == 0


async def test_graphignore_changes_invalidate_cache_and_totals(tmp_path: Path):
    """A .graphignore edit is visible on the next build, without the old
    30-second TTL; totals describe the filtered corpus, not ignored files."""
    from mcp_server.helpers import graphify_bridge as gb

    for name in ("keep.py", "skip.py", "later.py"):
        (tmp_path / name).write_text(f"def {name[:-3]}():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)

    initial = gb.build_graph(ws, chunk_size=0, include_html=False)
    assert initial["success"] is True

    (tmp_path / ".graphignore").write_text("skip.py\nlater.py\n", encoding="utf-8")
    filtered = gb.build_graph(ws, chunk_size=1, include_html=False)
    assert filtered["success"] is True, filtered
    assert filtered["total_files"] == 1
    assert filtered["files"] == 1
    assert filtered["remaining_files"] == 0

    exclusions = gb._gitignore_exclusions(tmp_path)
    assert gb._gitignored(tmp_path, tmp_path / "skip.py", exclusions) is True
    assert gb._gitignored(tmp_path, tmp_path / "keep.py", exclusions) is False
