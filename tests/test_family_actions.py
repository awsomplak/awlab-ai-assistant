"""family_info / family_config action guarantees (plan 2.x / task 7.2).

Locks in:
1. ``family_config op=create`` writes a v2 ``project-families.json``; duplicate
   create is rejected.
2. ``family_info`` resolves the PRIMARY family + ``workspace_families`` (multi-family
   safe) and seeds/updates the per-project ``.ai/family-id`` marker.
3. Validation rejects bad slugs / relative member paths / duplicate ``project_id``.
4. ``family_config`` mutations re-seed ``.ai/family-id``; leaving the last family
   clears the marker.
"""

import json
from pathlib import Path

import pytest

from mcp_server.config import settings
from mcp_server.modules import registration


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config_home at a temp dir so tests never touch the real
    ~/.awlab-id/agent-memory/project-families.json."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(settings.__dict__, "config_home", cfg)
    return cfg


def _tools():
    return registration.mcp._tool_manager._tools


async def action_call(action: str, params: dict | None = None) -> dict:
    tool = _tools()["action_call"]
    params = dict(params or {})
    if action == "graph_build":
        params.setdefault("background", False)
    return json.loads(await tool.fn(action=action, params=params))


def _mkproject(tmp_path, name: str, pid: str) -> Path:
    p = tmp_path / name
    (p / ".ai").mkdir(parents=True, exist_ok=True)
    (p / ".ai" / "project-id").write_text(pid, encoding="utf-8")
    return p


async def test_family_config_create_and_duplicate_reject(isolated_config, tmp_path):
    proj = _mkproject(tmp_path, "frontend", "frontend")
    ws = str(proj)
    r = await action_call(
        "family_config",
        {
            "workspace_path": ws,
            "op": "create",
            "slug": "my_app",
            "name": "My App",
            "members": [{"path": ws, "project_id": "frontend"}],
        },
    )
    assert r["success"] is True
    assert r["result"]["action"] == "created"

    cfg = isolated_config / "project-families.json"
    assert cfg.is_file()
    raw = json.loads(cfg.read_text(encoding="utf-8"))
    assert raw["my_app"]["members"][0]["project_id"] == "frontend"

    dup = await action_call("family_config", {"workspace_path": ws, "op": "create", "slug": "my_app"})
    assert dup["success"] is True  # dispatched ok
    assert dup["result"]["success"] is False  # business-level failure
    assert "already exists" in dup["result"]["error"]


async def test_family_info_primary_and_multifamily(isolated_config, tmp_path):
    a = _mkproject(tmp_path, "frontend", "frontend")
    b = _mkproject(tmp_path, "backend", "backend")
    c = _mkproject(tmp_path, "worker", "worker")
    ws = str(a)
    await action_call(
        "family_config",
        {"workspace_path": ws, "op": "create", "slug": "fam_a", "members": [{"path": str(a)}, {"path": str(b)}]},
    )
    await action_call(
        "family_config",
        {"workspace_path": ws, "op": "create", "slug": "fam_b", "members": [{"path": str(a)}, {"path": str(c)}]},
    )

    fi = await action_call("family_info", {"workspace_path": ws})
    res = fi["result"]
    assert res["family_id"] == "fam_a"  # primary = first declared match
    assert res["workspace_family"]["slug"] == "fam_a"
    assert {f["slug"] for f in res["workspace_families"]} == {"fam_a", "fam_b"}
    assert (Path(ws) / ".ai" / "family-id").read_text(encoding="utf-8").strip() == "fam_a"


async def test_family_validation(isolated_config, tmp_path):
    a = _mkproject(tmp_path, "frontend", "frontend")
    b = _mkproject(tmp_path, "other", "other")
    ws = str(a)

    e1 = await action_call(
        "family_config",
        {"workspace_path": ws, "op": "create", "slug": "app1", "members": [{"path": "relative/path"}]},
    )
    assert e1["result"]["success"] is False and "absolute" in e1["result"]["error"]

    e2 = await action_call(
        "family_config",
        {
            "workspace_path": ws,
            "op": "create",
            "slug": "app2",
            "members": [{"path": str(a), "project_id": "dup"}, {"path": str(b), "project_id": "dup"}],
        },
    )
    assert e2["result"]["success"] is False and "duplicate project_id" in e2["result"]["error"]

    e3 = await action_call("family_config", {"workspace_path": ws, "op": "create", "slug": "BAD SLUG"})
    assert e3["success"] is False


async def test_family_marker_lifecycle(isolated_config, tmp_path):
    a = _mkproject(tmp_path, "frontend", "frontend")
    b = _mkproject(tmp_path, "backend", "backend")
    ws = str(a)
    await action_call(
        "family_config",
        {"workspace_path": ws, "op": "create", "slug": "fam", "members": [{"path": str(a)}, {"path": str(b)}]},
    )
    mark = Path(ws) / ".ai" / "family-id"
    assert mark.read_text(encoding="utf-8").strip() == "fam"

    # Remove this project from the family → marker cleared by the mutation handler.
    rm = await action_call(
        "family_config", {"workspace_path": ws, "op": "remove_member", "slug": "fam", "path": str(a)}
    )
    assert rm["success"] is True
    assert rm["result"].get("family_id") is None
    assert not mark.exists()
