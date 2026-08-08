"""
Phase 9 — Verify graph_* Guarantees.

Locks in:

1. Single-request flow — a graph read with no prior build auto-builds; after a
   source edit the next read auto-updates (not just no-op); the trace reports
   the executed steps.
2. Per-project isolation — two projects get separate .ai/codegraph/ dirs and
   never cross-contaminate (queries in project A never see project B symbols).

Uses the same direct-tool-call pattern as test_dispatcher_surface.py.
"""

import asyncio
import json
import time
from pathlib import Path

from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    return json.loads(await tool.fn(action=action, params=params))


# ── Task 1: single-request flow ─────────────────────────────────────────────


async def test_single_request_auto_builds_then_auto_updates(tmp_path: Path):
    """Read with no build → auto-build (executed); edit a file → next read auto-updates."""
    sample = tmp_path / "sample.py"
    sample.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)

    # 1. No prior build → read auto-builds.
    r1 = await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    assert r1["success"] is True
    assert "graph_fresh" in r1["executed"]
    assert r1["result"]["count"] >= 1
    assert (tmp_path / ".ai" / "codegraph" / "graph.json").is_file()

    # 2. Fresh read (no change) → nothing re-runs.
    r2 = await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    assert r2["executed"] == []
    assert "graph_fresh" in r2["skipped"]

    # 3. Edit a source file → next read auto-updates (executed again).
    sample.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
    r3 = await action_call("graph_query", {"workspace_path": ws, "query": "bar"})
    assert r3["success"] is True
    assert "graph_fresh" in r3["executed"]  # stale → rebuild ran
    assert r3["result"]["count"] >= 1  # new symbol now searchable


async def test_single_request_edit_surfaces_new_symbol(tmp_path: Path):
    """After an edit, a previously-unknown symbol becomes queryable."""
    sample = tmp_path / "sample.py"
    sample.write_text("def foo():\n    return 1\n", encoding="utf-8")
    ws = str(tmp_path)

    await action_call("graph_query", {"workspace_path": ws, "query": "foo"})
    before = await action_call("graph_query", {"workspace_path": ws, "query": "brand_new_sym"})
    assert before["result"]["count"] == 0

    sample.write_text("def brand_new_sym():\n    return 42\n", encoding="utf-8")
    after = await action_call("graph_query", {"workspace_path": ws, "query": "brand_new_sym"})
    assert after["success"] is True
    assert after["result"]["count"] >= 1


# ── Task 2: per-project isolation ───────────────────────────────────────────


async def test_per_project_isolation(tmp_path: Path):
    """Two projects get separate .ai/codegraph/ and never cross-contaminate."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / "alpha.py").write_text("def alpha_func():\n    return 'a'\n", encoding="utf-8")
    (proj_b / "beta.py").write_text("def beta_func():\n    return 'b'\n", encoding="utf-8")

    wa, wb = str(proj_a), str(proj_b)

    ra = await action_call("graph_query", {"workspace_path": wa, "query": "alpha_func"})
    rb = await action_call("graph_query", {"workspace_path": wb, "query": "beta_func"})
    assert ra["success"] is True and rb["success"] is True
    assert ra["result"]["count"] >= 1
    assert rb["result"]["count"] >= 1

    # A must NOT see B's symbol, and vice-versa.
    ra_beta = await action_call("graph_query", {"workspace_path": wa, "query": "beta_func"})
    rb_alpha = await action_call("graph_query", {"workspace_path": wb, "query": "alpha_func"})
    assert ra_beta["result"]["count"] == 0
    assert rb_alpha["result"]["count"] == 0

    # Separate codegraph dirs exist.
    assert (proj_a / ".ai" / "codegraph" / "graph.json").is_file()
    assert (proj_b / ".ai" / "codegraph" / "graph.json").is_file()


async def test_graph_status_reports_existence_per_project(tmp_path: Path):
    """graph_status reflects each project independently (exists/fresh)."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    (proj_a / "alpha.py").write_text("def alpha_func():\n    return 1\n", encoding="utf-8")

    # Only project A has been built.
    await action_call("graph_query", {"workspace_path": str(proj_a), "query": "alpha_func"})

    sa = await action_call("graph_status", {"workspace_path": str(proj_a)})
    sb = await action_call("graph_status", {"workspace_path": str(proj_b)})
    assert sa["result"].get("exists") is True
    assert sb["result"].get("exists") is False


# ── Task 3: incremental rebuild ─────────────────────────────────────────────


def _codegraph(proj: Path) -> dict:
    """Load graph.json from a project's .ai/codegraph dir."""
    return json.loads((proj / ".ai" / "codegraph" / "graph.json").read_text(encoding="utf-8"))


def _make_multi_file_project(tmp_path: Path, name: str = "incr") -> tuple[Path, str]:
    proj = tmp_path / name
    proj.mkdir()
    (proj / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (proj / "b.py").write_text("def beta():\n    return alpha()\n", encoding="utf-8")
    (proj / "c.py").write_text("def gamma():\n    return 2\n", encoding="utf-8")
    return proj, str(proj)


async def test_incremental_rebuild_after_edit(tmp_path: Path):
    """Edit one file → next build re-extracts only it (incremental=True) with no drift."""
    proj, ws = _make_multi_file_project(tmp_path)

    first = await action_call("graph_build", {"workspace_path": ws})
    assert first["success"] is True
    assert first["result"].get("incremental") is False  # first build is full

    # Edit a single file (different content, same second is fine — ns manifest).
    (proj / "b.py").write_text("def beta():\n    return alpha() * 2\n", encoding="utf-8")
    second = await action_call("graph_build", {"workspace_path": ws})
    assert second["success"] is True
    assert second["result"].get("incremental") is True  # incremental path taken
    assert second["result"].get("nodes") == first["result"].get("nodes")  # no node drift

    # No synthetic global 'any' node leaked in.
    g = _codegraph(proj)
    assert not any(n.get("id") == "any" for n in g["nodes"])
    # Cross-file edge (beta -> alpha) still present.
    assert any(e.get("source", "").endswith("_beta") for e in g["links"])


async def test_incremental_no_change_skips(tmp_path: Path):
    """Rebuild with no source change → fast skip (nothing re-extracted)."""
    proj, ws = _make_multi_file_project(tmp_path)

    await action_call("graph_build", {"workspace_path": ws})
    again = await action_call("graph_build", {"workspace_path": ws})
    assert again["success"] is True
    assert again["result"].get("skipped") is True  # fresh → no-op
    assert again["result"].get("incremental") is False


async def test_incremental_delete_removes_stale_nodes(tmp_path: Path):
    """Deleting a file drops its nodes on the next incremental build (no stale ghost)."""
    proj, ws = _make_multi_file_project(tmp_path)

    await action_call("graph_build", {"workspace_path": ws})

    # Add a file, build (incremental add), then delete it and rebuild.
    (proj / "d.py").write_text("def delta():\n    return 3\n", encoding="utf-8")
    added = await action_call("graph_build", {"workspace_path": ws})
    assert added["result"].get("incremental") is True
    assert any(n["id"].endswith("_delta") for n in _codegraph(proj)["nodes"])

    (proj / "d.py").unlink()
    removed = await action_call("graph_build", {"workspace_path": ws})
    assert removed["result"].get("incremental") is True
    g = _codegraph(proj)
    assert not any(n["id"].endswith("_delta") for n in g["nodes"])  # stale node gone


async def test_incremental_matches_full_rebuild(tmp_path: Path):
    """At the same source state, incremental output == a forced full rebuild output."""
    proj, ws = _make_multi_file_project(tmp_path)

    # Build full, edit one file, build incremental.
    await action_call("graph_build", {"workspace_path": ws})
    (proj / "b.py").write_text("def beta():\n    return alpha() * 3\n", encoding="utf-8")
    inc = await action_call("graph_build", {"workspace_path": ws})
    assert inc["result"].get("incremental") is True
    inc_g = _codegraph(proj)

    # Force a full rebuild of the SAME state (delete manifest + graph.json).
    for f in (".build_state.json", "graph.json", "graph.html"):
        p = proj / ".ai" / "codegraph" / f
        if p.exists():
            p.unlink()
    full = await action_call("graph_build", {"workspace_path": ws})
    assert full["result"].get("incremental") is False
    full_g = _codegraph(proj)

    def node_key(g):
        return {(n.get("source_file") or "", n["id"]) for n in g["nodes"]}

    def edge_key(g):
        return {(e.get("source"), e.get("relation"), e.get("target")) for e in g["links"]}

    assert node_key(inc_g) == node_key(full_g)
    assert edge_key(inc_g) == edge_key(full_g)


# ── Task 4: scratch/temp files are never indexed ────────────────────────────


async def test_scratch_dir_excluded_from_graph(tmp_path: Path):
    """Files under .ai/temp/ (scratch) must never appear in the code graph."""
    proj = tmp_path / "scratch_proj"
    proj.mkdir()
    (proj / "real.py").write_text("def real_func():\n    return 1\n", encoding="utf-8")
    scratch = proj / ".ai" / "temp"
    scratch.mkdir(parents=True)
    (scratch / "scratch_check.py").write_text("def scratch_only():\n    return 99\n", encoding="utf-8")
    ws = str(proj)

    r = await action_call("graph_build", {"workspace_path": ws})
    assert r["success"] is True

    g = _codegraph(proj)
    ids = [n["id"] for n in g["nodes"]]
    assert any(i.endswith("_real_func") for i in ids)  # real code indexed
    assert not any("scratch" in i or i.endswith("_scratch_only") for i in ids)  # scratch excluded
    # No node may carry a source_file under .ai/temp
    assert not any((n.get("source_file") or "").startswith(".ai/temp") for n in g["nodes"])


# ── Task 5: heavy stale rebuilds run in the background (non-blocking) ───────


async def test_heavy_stale_rebuild_runs_in_background(tmp_path: Path):
    """A stale graph with >= threshold changed files rebuilds in the background.

    The graph read returns immediately (no blocking rebuild); the graph becomes
    fresh once the background thread finishes.
    """
    proj = tmp_path / "bg_proj"
    proj.mkdir()
    n = 24  # >= _BACKGROUND_THRESHOLD (20)
    for i in range(n):
        (proj / f"m{i:02d}.py").write_text(f"def m{i:02d}():\n    return {i}\n", encoding="utf-8")
    ws = str(proj)

    # First build is synchronous (no graph exists yet).
    r1 = await action_call("graph_build", {"workspace_path": ws})
    assert r1["success"] is True

    # Touch ALL files → the next read sees a heavy stale state.
    for i in range(n):
        (proj / f"m{i:02d}.py").write_text(f"def m{i:02d}():\n    return {i + 100}\n", encoding="utf-8")

    t0 = time.perf_counter()
    r2 = await action_call("graph_query", {"workspace_path": ws, "query": "m00"})
    elapsed = time.perf_counter() - t0
    assert r2["success"] is True
    assert elapsed < 10, f"graph read blocked on a full rebuild: {elapsed:.1f}s"
    # The agent can now SEE it read stale data (and that a rebuild is in flight),
    # so it won't silently act on outdated structure.
    assert r2["result"].get("graph_fresh") is False
    assert r2["result"].get("graph_rebuilding") is True

    # The background rebuild eventually completes → graph becomes fresh.
    deadline = time.perf_counter() + 60
    fresh = False
    while time.perf_counter() < deadline:
        st = await action_call("graph_status", {"workspace_path": ws})
        if st["result"].get("fresh"):
            fresh = True
            break
        await asyncio.sleep(0.5)
    assert fresh, "background rebuild never completed"

    # A read now reports fresh data.
    r3 = await action_call("graph_query", {"workspace_path": ws, "query": "m00"})
    assert r3["result"].get("graph_fresh") is True


def test_graph_build_coalesces_with_background_rebuild(tmp_path: Path):
    """Explicit graph_build during an in-flight background rebuild must not
    double-build — it returns ``rebuilding: True`` immediately."""
    from mcp_server.helpers import graphify_bridge as gb

    proj = tmp_path / "coalesce"
    proj.mkdir()
    n = 24
    for i in range(n):
        (proj / f"m{i:02d}.py").write_text(f"def m{i:02d}():\n    return {i}\n", encoding="utf-8")
    ws = str(proj)

    # First build (synchronous).
    assert gb.build_graph(ws).get("success") is True

    # Heavy stale → background rebuild starts.
    for i in range(n):
        (proj / f"m{i:02d}.py").write_text(f"def m{i:02d}():\n    return {i + 1}\n", encoding="utf-8")
    r = gb.ensure_fresh(ws, background=True)
    assert r.get("background") is True
    assert gb._rebuild_in_flight(ws) is True

    # Explicit build while it runs → coalesce (no duplicate/block).
    r2 = gb.graph_build_action(ws)
    assert r2.get("rebuilding") is True

    # Wait for the background build to finish → fresh.
    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline and not gb.graph_status(ws).get("fresh"):
        time.sleep(0.2)
    assert gb.graph_status(ws).get("fresh") is True
