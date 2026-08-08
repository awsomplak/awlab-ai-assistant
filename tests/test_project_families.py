"""
Tests for Phase 13 — project-family support (plan 2l6iavva).

Covers:
1. ``store_target`` — maps project/patterns/family_<slug> to (patterns, family).
2. ``family_root`` — nearest common ancestor of correlated member paths.
3. ``family_for_workspace`` — resolves a member workspace to its family slug.
4. Family memory isolation — mem_write/mem_search with ``store="family_<slug>"``
   land in a dedicated family DB, isolated from the project store.
5. Family graph root — graph ops with ``family`` resolve to the family root so
   backend↔frontend edges span correlated projects.
"""

import json
from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.helpers import agent_recall
from mcp_server.helpers.agent_recall import (
    family_for_workspace,
    family_member_id,
    family_root,
    seed_member_project_id,
    store_target,
    sync_family_project_ids,
)
from mcp_server.helpers.graphify_bridge import (
    _codegraph_dir,
    _family_codegraph_dir,
    _resolve_graph_root,
    graph_build_action,
    query_graph,
)
from mcp_server.modules import registration


def _tools() -> dict:
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    return json.loads(await tool.fn(action=action, params=params))


def _make_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / name
    (proj / ".ai").mkdir(parents=True, exist_ok=True)
    (proj / ".ai" / "project-id").write_text(f"proj_{name}", encoding="utf-8")
    return proj


@pytest.fixture
def isolated_families(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point family declaration + family DBs at a temp dir (never the real config home)."""
    monkeypatch.setattr(agent_recall, "project_families_path", lambda: tmp_path / "project-families.json")
    monkeypatch.setattr(agent_recall, "family_db_path", lambda slug: str(tmp_path / f"family_{slug}.db"))
    return tmp_path


def _write_families(tmp_path: Path, families: dict) -> None:
    """Write a family declaration (v1 array or v2 object-with-members)."""
    (tmp_path / "project-families.json").write_text(json.dumps(families), encoding="utf-8")


# ── 1. store_target mapping ────────────────────────────────────────────────


def test_store_target_mapping():
    assert store_target("project") == (False, None)
    assert store_target("patterns") == (True, None)
    assert store_target("family_webapp") == (False, "webapp")
    assert store_target("family_backend-api") == (False, "backend-api")


# ── 2. family_root: nearest common ancestor ────────────────────────────────


def test_family_root_common_ancestor(tmp_path: Path, isolated_families):
    root = tmp_path / "repos"
    a = root / "backend"
    b = root / "frontend"
    (a / ".ai").mkdir(parents=True, exist_ok=True)
    (b / ".ai").mkdir(parents=True, exist_ok=True)
    _write_families(tmp_path, {"webapp": [str(a), str(b)]})
    assert family_root("webapp") == root.resolve()


def test_family_root_unknown_slug_returns_none(tmp_path: Path, isolated_families):
    _write_families(tmp_path, {})
    assert family_root("nope") is None


# ── 3. family_for_workspace ────────────────────────────────────────────────


def test_family_for_workspace(tmp_path: Path, isolated_families):
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _write_families(tmp_path, {"pair": [str(a), str(b)]})
    assert family_for_workspace(a) == "pair"
    assert family_for_workspace(b) == "pair"
    assert family_for_workspace(tmp_path / "other") is None


# ── 4. Family memory isolation (via action surface) ────────────────────────


async def test_family_memory_isolation_via_actions(tmp_path: Path, isolated_families):
    proj = _make_project(tmp_path, "fam1")
    ws = str(proj)
    _write_families(tmp_path, {"webapp": [ws]})

    # Write a cross-project entity to the family store.
    w = await action_call(
        "mem_write",
        {
            "workspace_path": ws,
            "store": "family_webapp",
            "entities": [{"name": "SharedDomain", "entityType": "concept", "observations": ["shared across members"]}],
        },
    )
    assert w["success"] is True
    assert w["result"]["store"] == "family_webapp"

    # Read it back from the family store.
    r = await action_call(
        "mem_search",
        {"workspace_path": ws, "store": "family_webapp", "query": "SharedDomain"},
    )
    names = [e.get("name") for e in r["result"]["data"]]
    assert "SharedDomain" in names

    # The project store is isolated — it does NOT see the family entity.
    rp = await action_call(
        "mem_search",
        {"workspace_path": ws, "store": "project", "query": "SharedDomain"},
    )
    proj_names = [e.get("name") for e in rp["result"]["data"]]
    assert "SharedDomain" not in proj_names


# ── 5. Family graph root resolution ────────────────────────────────────────


def test_family_graph_root_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    member = tmp_path / "member"
    member.mkdir(parents=True)
    # Isolate the family codegraph dir away from the real config home.
    monkeypatch.setattr(settings, "config_home", tmp_path / "cfg")

    resolved = _resolve_graph_root(member, None, family="webapp")
    assert resolved == tmp_path / "cfg" / "codegraph" / "family_webapp"

    # Without family, falls back to the workspace root.
    assert _resolve_graph_root(member) == member.resolve()


# ── 6. Native family graph build (per-member roots + tag merge) ───────────


def test_family_native_build_combined_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Family graph = per-member per-project graphs merged with member:: tags.

    Builds two tiny member projects, builds the family graph, and verifies:
    clean per-member source_file labels, repo-tagged node IDs, combined search,
    and NO fabricated cross-project edge.
    """
    monkeypatch.setattr(settings, "config_home", tmp_path / "cfg")
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)

    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "b.js").write_text("export function apiHandler() { return 7 }\n", encoding="utf-8")
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "a.js").write_text(
        "import { helper } from './lib'\nexport const a = helper()\n", encoding="utf-8"
    )
    (frontend / "src" / "lib.js").write_text("export function helper() { return 42 }\n", encoding="utf-8")

    (tmp_path / "cfg" / "project-families.json").write_text(
        json.dumps(
            {
                "webapp": {
                    "name": "Webapp",
                    "members": [
                        {"path": str(backend), "project_id": "backend"},
                        {"path": str(frontend), "project_id": "frontend"},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    # Build the merged family graph from the frontend member's workspace.
    res = graph_build_action(workspace_path=str(frontend), family="webapp", include_html=True)
    assert res["success"] is True, res
    assert res["nodes"] == 6  # backend 2 + frontend 4
    assert res["edges"] == 5
    assert res["members"] == 2
    assert res["family_html_mirrored_to_members"] is True

    # Family graph lives under the isolated config home (not a member dir).
    fam_dir = _codegraph_dir(_family_codegraph_dir("webapp"))
    family_graph = fam_dir / "graph.json"
    assert family_graph.exists()
    # Two HTMLs: family.html (combined) generated in the family dir…
    assert (fam_dir / "family.html").is_file()
    # …and mirrored into EVERY member's .ai/codegraph/family.html (identical copy).
    for member in (backend, frontend):
        member_out = member / ".ai" / "codegraph"
        assert (member_out / "family.html").is_file(), f"missing family.html in {member}"
        assert (member_out / "graph.html").is_file(), f"missing own graph.html in {member}"
        assert (member_out / "family.html").read_bytes() == (fam_dir / "family.html").read_bytes()

    data = json.loads(family_graph.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    ids = [n.get("id") for n in nodes]
    # Every node is tagged with its member tag.
    assert any(i.startswith("backend::") for i in ids)
    assert any(i.startswith("frontend::") for i in ids)
    # source_file labels stay clean per-member (no drive/root mangling).
    assert any(n.get("source_file") == "src/a.js" and n.get("repo") == "frontend" for n in nodes)
    assert any(n.get("source_file") == "app/b.js" and n.get("repo") == "backend" for n in nodes)

    # Combined search finds backend symbols from the frontend workspace.
    q = query_graph(str(frontend), "apiHandler", family="webapp", limit=5)
    assert q.get("count", 0) >= 1
    assert any(r.get("repo") == "backend" for r in q.get("results", []))

    # No fabricated cross-project edge (backend <-> frontend) exists.
    from mcp_server.helpers.graphify_bridge import _load_nx_graph

    G = _load_nx_graph(family_graph)
    backend_ids = {n for n in G.nodes if str(n).startswith("backend::")}
    frontend_ids = {n for n in G.nodes if str(n).startswith("frontend::")}
    for u in backend_ids:
        assert not (set(G.neighbors(u)) & frontend_ids), f"unexpected cross-project edge {u}"


# ── 7. Member identity: declared project_id > .ai/project-id > derived + seed ──


def test_family_member_id_and_seed(tmp_path: Path, isolated_families):
    member = tmp_path / "member"
    member.mkdir(parents=True)
    _write_families(
        tmp_path,
        {"eka-warehouse": {"name": "Eka", "members": [{"path": str(member), "project_id": "eka-warehouse-frontend"}]}},
    )

    # Declared project_id wins.
    assert family_member_id("eka-warehouse", member) == "eka-warehouse-frontend"
    # Seeding writes .ai/project-id for the (fresh) project.
    assert seed_member_project_id("eka-warehouse", member) == "eka-warehouse-frontend"
    assert (member / ".ai" / "project-id").read_text(encoding="utf-8") == "eka-warehouse-frontend"
    # Idempotent — a second seed does not overwrite.
    assert seed_member_project_id("eka-warehouse", member) == "eka-warehouse-frontend"
    assert (member / ".ai" / "project-id").read_text(encoding="utf-8") == "eka-warehouse-frontend"

    # An existing .ai/project-id wins over derivation.
    other = tmp_path / "other"
    (other / ".ai").mkdir(parents=True)
    (other / ".ai" / "project-id").write_text("custom_id", encoding="utf-8")
    assert family_member_id("eka-warehouse", other) == "custom_id"

    # Derived fallback: <slug>-<dir> when nothing is declared/present.
    fresh = tmp_path / "fresh"
    fresh.mkdir(parents=True)
    assert family_member_id("eka-warehouse", fresh) == "eka-warehouse-fresh"


def test_family_member_id_forward_slash_json_path(tmp_path: Path, isolated_families):
    """Live JSON uses forward slashes (D:/Project/...) — declared id must still win.

    Regression: on Windows ``Path.resolve()`` yields backslashes, so the declared
    lookup used to miss forward-slash keys and fall back to the derived id
    (e.g. ``eka-warehouse-eka_warehouse_backend``) instead of ``eka-warehouse-backend``.
    """
    member = tmp_path / "eka-warehouse-backend"
    member.mkdir(parents=True)
    posix_path = str(member).replace("\\", "/")
    _write_families(
        tmp_path,
        {"eka-warehouse": {"name": "Eka", "members": [{"path": posix_path, "project_id": "eka-warehouse-backend"}]}},
    )
    # Declared id wins (NOT the derived 'eka-warehouse-eka_warehouse_backend').
    assert family_member_id("eka-warehouse", member) == "eka-warehouse-backend"
    # Seeding writes the declared id, not the derived fallback.
    assert seed_member_project_id("eka-warehouse", member) == "eka-warehouse-backend"
    assert (member / ".ai" / "project-id").read_text(encoding="utf-8") == "eka-warehouse-backend"


# ── 8. Project-id is authoritative + family JSON reconciliation + dupes ──────


def test_family_member_id_file_wins_over_declared(tmp_path: Path, isolated_families):
    member = tmp_path / "member"
    (member / ".ai").mkdir(parents=True)
    (member / ".ai" / "project-id").write_text("eka_warehouse", encoding="utf-8")
    _write_families(
        tmp_path,
        {"eka-warehouse": {"members": [{"path": str(member), "project_id": "eka-warehouse-frontend"}]}},
    )
    # The project's own .ai/project-id is authoritative over the declared id.
    assert family_member_id("eka-warehouse", member) == "eka_warehouse"


def test_sync_family_project_ids_reconciles_and_detects_dupes(tmp_path: Path, isolated_families):
    # Member A: project id file differs from declared (hyphen vs underscore).
    a = tmp_path / "a"
    (a / ".ai").mkdir(parents=True)
    (a / ".ai" / "project-id").write_text("eka_warehouse_frontend", encoding="utf-8")
    # Member B: no file, but its DECLARED id collides with A's effective id.
    b = tmp_path / "b"
    b.mkdir(parents=True)
    # Member C: fresh (no file) — keeps its declared id.
    c = tmp_path / "c"
    c.mkdir(parents=True)

    _write_families(
        tmp_path,
        {
            "eka-warehouse": {
                "members": [
                    {"path": str(a), "project_id": "eka-warehouse-frontend"},
                    {"path": str(b), "project_id": "eka_warehouse_frontend"},
                    {"path": str(c), "project_id": "eka-warehouse-desktop"},
                ]
            }
        },
    )

    res = sync_family_project_ids("eka-warehouse")
    assert res["updated"] is True
    # A's declared id was reconciled to its project file (changed).
    assert any("eka_warehouse_frontend" in ch for ch in res["changes"])
    # B collided with A's effective id -> duplicate checker cleared B's declared id.
    assert res["conflicts"] and "duplicate project_id" in res["conflicts"][0]

    # Reload the reconciled file: A = file id, B = cleared (derives), C = declared.
    raw = json.loads((tmp_path / "project-families.json").read_text(encoding="utf-8"))
    m = {str(m["path"]): m for m in raw["eka-warehouse"]["members"]}
    assert m[str(a)]["project_id"] == "eka_warehouse_frontend"
    assert m[str(b)]["project_id"] == ""
    assert m[str(c)]["project_id"] == "eka-warehouse-desktop"
    # Derived distinct id for the duplicate member (no file, declared cleared).
    assert family_member_id("eka-warehouse", b) == "eka-warehouse-b"

    # Second sync is a no-op (nothing to reconcile).
    res2 = sync_family_project_ids("eka-warehouse")
    assert res2["updated"] is False
    assert res2["conflicts"] == []
