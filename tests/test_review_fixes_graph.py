"""
Regression tests for the live-test review GRAPH findings (plan 2l6iavva, Phases 1-2).

Locks in the fixes:
1. ``graph_build`` relative ``root`` scoping — ``root="src"`` resolves against
   ``workspace_path`` (not the server CWD) and scans only the subdirectory.
2. Unified node identity — ``graph_path``/``graph_explain`` accept the node ids
   and file paths returned by ``graph_query`` (not just labels).
3. Cross-file navigation — a path resolves between symbols in different files
   (module/file-level connectivity).
4. Diagnostics — ``node(s) not found`` vs ``no path found`` are distinguished;
   a ``no_path`` failure carries both source files.
5. Identifier fallback — ``graph_query`` returns whole-word source hits
   (``mode="identifier"``) for variable-level identifiers (ref/computed/prop).

Uses the direct-tool-call pattern (no stdio).
"""

import json
from pathlib import Path

from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    return json.loads(await tool.fn(action=action, params=params))


def _make_vue_src_project(tmp_path: Path, name: str) -> Path:
    """Vue-style project: src/stores + src/pages + a stray root JS file.

    The stray root file proves that ``root="src"`` scopes to src/ only.
    """
    proj = tmp_path / name
    (proj / "src" / "stores").mkdir(parents=True, exist_ok=True)
    (proj / "src" / "pages").mkdir(parents=True, exist_ok=True)
    (proj / "tool.js").write_text("export function rootTool() {\n  return 1;\n}\n", encoding="utf-8")
    (proj / "src" / "stores" / "auth.js").write_text(
        "export function useAuthStore() {\n  return { token: null, login() { this.token = 'x'; } };\n}\n",
        encoding="utf-8",
    )
    (proj / "src" / "pages" / "DashboardPage.vue").write_text(
        "<script setup>\n"
        "import { useAuthStore } from '../stores/auth';\n"
        "const auth = useAuthStore();\n"
        "const brakeBaselineDays = 30;\n"
        "const dashboardPermission = 'admin';\n"
        "</script>\n",
        encoding="utf-8",
    )
    return proj


# ── 1. graph_build relative root scoping ────────────────────────────────────


async def test_graph_build_relative_root_scopes_to_subdir(tmp_path: Path):
    proj = _make_vue_src_project(tmp_path, "rootscope")
    ws = str(proj)

    # Whole project (no root): 3 files (tool.js + 2 under src/).
    whole = await action_call("graph_build", {"workspace_path": ws})
    assert whole["result"]["success"] is True
    assert whole["result"]["files"] == 3

    # Absolute root → src/: 2 files.
    abs_build = await action_call("graph_build", {"workspace_path": ws, "root": str(proj / "src")})
    assert abs_build["result"]["success"] is True
    assert abs_build["result"]["files"] == 2

    # Relative root="src" must resolve against workspace_path → also 2 files,
    # NOT 3 (and NOT "no supported source files detected").
    rel_build = await action_call("graph_build", {"workspace_path": ws, "root": "src"})
    assert rel_build["result"]["success"] is True
    assert rel_build["result"]["files"] == 2


# ── 2. unified node identity: ids + file paths in path/explain ──────────────


async def test_graph_path_accepts_node_ids_and_file_paths(tmp_path: Path):
    proj = _make_vue_src_project(tmp_path, "identity")
    ws = str(proj)
    await action_call("graph_build", {"workspace_path": ws, "root": "src"})

    q = await action_call("graph_query", {"workspace_path": ws, "query": "useAuthStore"})
    assert q["result"]["count"] >= 1
    node_id = q["result"]["results"][0]["id"]

    # By label.
    by_label = await action_call("graph_path", {"workspace_path": ws, "a": "useAuthStore", "b": "DashboardPage.vue"})
    assert by_label["result"]["success"] is True

    # By node id returned from graph_query (previously "node(s) not found").
    by_id = await action_call("graph_path", {"workspace_path": ws, "a": node_id, "b": "DashboardPage.vue"})
    assert by_id["result"]["success"] is True
    assert by_id["result"]["hops"] >= 1

    # By file paths (cross-file navigation).
    by_files = await action_call(
        "graph_path",
        {"workspace_path": ws, "a": "src/stores/auth.js", "b": "src/pages/DashboardPage.vue"},
    )
    assert by_files["result"]["success"] is True

    # explain accepts the id too (identity is consistent across tools).
    ex = await action_call("graph_explain", {"workspace_path": ws, "node": node_id})
    assert ex["result"]["success"] is True
    assert ex["result"]["node"]["label"] == "useAuthStore()"


# ── 3. cross-file navigation (module connectivity) ──────────────────────────


async def test_graph_path_resolves_across_files(tmp_path: Path):
    proj = tmp_path / "cross"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "a.py").write_text("from b import helper_b\n\ndef func_a():\n    return helper_b()\n", encoding="utf-8")
    (proj / "b.py").write_text("def helper_b():\n    return 1\n\ndef other_b():\n    return 2\n", encoding="utf-8")
    ws = str(proj)
    await action_call("graph_build", {"workspace_path": ws})

    # func_a calls helper_b — direct symbol edge.
    r1 = await action_call("graph_path", {"workspace_path": ws, "a": "func_a", "b": "helper_b"})
    assert r1["result"]["success"] is True

    # func_a and other_b have no direct call edge but live in files connected by
    # an import — cross-file path must still resolve (module-level connectivity).
    r2 = await action_call("graph_path", {"workspace_path": ws, "a": "func_a", "b": "other_b"})
    assert r2["result"]["success"] is True
    assert r2["result"]["hops"] >= 1


# ── 4. diagnostics: node-not-found vs no-path ───────────────────────────────


async def test_graph_path_distinguishes_not_found_vs_no_path(tmp_path: Path):
    proj = _make_vue_src_project(tmp_path, "diag")
    ws = str(proj)
    await action_call("graph_build", {"workspace_path": ws, "root": "src"})

    # Unknown term → "node(s) not found" listing the missing term.
    r = await action_call("graph_path", {"workspace_path": ws, "a": "zzz_missing", "b": "DashboardPage.vue"})
    assert r["result"]["success"] is False
    assert "node(s) not found" in r["result"]["error"]

    # Two symbols in files with NO relationship → "no path found" with source files.
    proj2 = tmp_path / "disconnected"
    proj2.mkdir(parents=True, exist_ok=True)
    (proj2 / "x.py").write_text("def only_x():\n    return 1\n", encoding="utf-8")
    (proj2 / "y.py").write_text("def only_y():\n    return 2\n", encoding="utf-8")
    ws2 = str(proj2)
    await action_call("graph_build", {"workspace_path": ws2})
    r2 = await action_call("graph_path", {"workspace_path": ws2, "a": "only_x", "b": "only_y"})
    assert r2["result"]["success"] is False
    assert "no path found" in r2["result"]["error"]
    assert r2["result"]["no_path"] is True
    assert r2["result"]["a"]["source_file"] == "x.py"
    assert r2["result"]["b"]["source_file"] == "y.py"


# ── 5. identifier fallback (variable-level search) ──────────────────────────


async def test_graph_query_identifier_fallback_for_variables(tmp_path: Path):
    proj = _make_vue_src_project(tmp_path, "ident")
    ws = str(proj)
    await action_call("graph_build", {"workspace_path": ws, "root": "src"})

    # brakeBaselineDays is a ref/computed-style variable, NOT a graph node.
    # The identifier fallback must return file-level hits, not a dead end.
    r = await action_call("graph_query", {"workspace_path": ws, "query": "brakeBaselineDays"})
    assert r["result"]["success"] is True
    assert r["result"]["count"] >= 1
    assert r["result"]["mode"] == "identifier"
    assert r["result"]["results"][0]["type"] == "identifier"
    assert "DashboardPage.vue" in r["result"]["results"][0]["source_file"]

    # Node queries still behave normally (mode == "node").
    r2 = await action_call("graph_query", {"workspace_path": ws, "query": "useAuthStore"})
    assert r2["result"]["success"] is True
    assert r2["result"]["mode"] == "node"
    assert r2["result"]["count"] >= 1

    # A term that exists nowhere still returns 0.
    r3 = await action_call("graph_query", {"workspace_path": ws, "query": "zzz_definitely_missing"})
    assert r3["result"]["count"] == 0


# ── 6. gitignore + lock-file exclusion (no junk, no false staleness) ────────


async def test_graph_build_excludes_gitignore_and_lock_files(tmp_path: Path):
    proj = _make_vue_src_project(tmp_path, "exclude")
    ws = str(proj)
    # Committed junk graphify WOULD otherwise see: a gitignored dist/ dir + a
    # generated package-lock.json (committed, NOT gitignored).
    (proj / "dist").mkdir(parents=True, exist_ok=True)
    (proj / "dist" / "bundle.js").write_text("export const bundle = 1\n", encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (proj / ".gitignore").write_text("dist/\n", encoding="utf-8")

    # Build the whole project: only the 3 real source files (tool.js + 2 in src)
    # — dist/ (gitignored) and package-lock.json (generated lock) are excluded.
    r = await action_call("graph_build", {"workspace_path": ws})
    assert r["result"]["success"] is True, r["result"].get("error")
    assert r["result"]["files"] == 3
    assert "bundle.js" not in json.dumps(r["result"])

    # Freshness ignores gitignored AND lock-file churn (no false staleness).
    (proj / "dist" / "bundle.js").write_text("export const bundle = 2\n", encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}\n', encoding="utf-8")
    st = await action_call("graph_status", {"workspace_path": ws})
    assert st["result"]["success"] is True
    assert st["result"]["fresh"] is True

    # A REAL source change flips it stale.
    (proj / "src" / "stores" / "auth.js").write_text(
        "export function useAuthStore() { return { token: 'y' }; }\n", encoding="utf-8"
    )
    st2 = await action_call("graph_status", {"workspace_path": ws})
    assert st2["result"]["fresh"] is False
