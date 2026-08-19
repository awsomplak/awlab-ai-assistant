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
    r1 = await action_call("graph_build", {"workspace_path": ws, "max_files": 2})
    assert r1["success"] is True, r1
    assert r1["result"]["partial"] is True
    assert r1["result"]["pending_files"] >= 6
    assert r1["result"]["files"] == 2
    st = await action_call("graph_status", {"workspace_path": ws})
    assert st["result"]["fresh"] is False

    # 2. chunk_size bounds every run — two more calls finish it (3 then 3).
    r2 = await action_call("graph_build", {"workspace_path": ws, "chunk_size": 3})
    assert r2["success"] is True, r2
    assert r2["result"]["chunked"] is True
    assert r2["result"]["processed_files"] == 3
    assert r2["result"]["remaining_files"] == 3

    r3 = await action_call("graph_build", {"workspace_path": ws, "chunk_size": 3})
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
    """background=True starts a queue worker that advances chunks until the graph
    is complete (Laravel-queue handler style)."""
    from mcp_server.helpers import graphify_bridge as gb

    proj = tmp_path / "chunkbg"
    proj.mkdir()
    n = 40
    for i in range(n):
        (proj / f"b{i:02d}.py").write_text(f"def b{i:02d}():\n    return {i}\n", encoding="utf-8")
    ws = str(proj)

    r = gb.graph_build_action(ws, chunk_size=10, background=True)
    assert r.get("success") is True, r
    assert r.get("chunked") is True
    remaining = int(r.get("remaining_files") or 0)
    assert remaining > 0
    assert r.get("background") is True
    assert r.get("background_started") is True

    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline and not gb.graph_status(ws).get("fresh"):
        time.sleep(0.2)
    st = gb.graph_status(ws)
    assert st.get("fresh") is True
    assert st.get("remaining_files", 0) == 0


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
