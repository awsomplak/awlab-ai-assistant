"""Tests for ``reg_update`` — the single registry.md CRUD action.

create (server-generated UUID) / update (status -> correct table, Date refresh,
Created At immutable) / delete (strict user approval via confirmed=true).
Also covers the immutable ``Created At`` column + legacy 4-column parsing.
"""

import re
from pathlib import Path

from mcp_server.helpers.file_utils import write_file_safe
from mcp_server.helpers.registry_utils import (
    create_registry_entry,
    parse_registry,
)
from mcp_server.registry import _reg_update

UUID_RE = re.compile(r"^[a-z0-9]{8}$")


async def test_reg_update_create_generates_uuid(temp_project_dir):
    result = await _reg_update(workspace_path=str(temp_project_dir), type="create", summary="Brand new plan")
    assert result["success"] is True
    uid = result["created_uuid"]
    assert UUID_RE.match(uid)
    assert result["table"] == "active"
    assert result["date"] and result["created_at"]

    registry = parse_registry(str(temp_project_dir))
    row = [e for e in registry["active"] if e["uuid"] == uid]
    assert row and row[0]["status"] == "⏹️"
    assert row[0]["created_at"] == row[0]["date"]


async def test_reg_update_update_moves_tables_preserves_created_at(temp_project_dir):
    created = create_registry_entry(str(temp_project_dir), summary="Plan")
    uid = created["created_uuid"]
    created_at = created["created_at"]

    # active -> paused
    res = await _reg_update(workspace_path=str(temp_project_dir), type="update", uuid=uid, status="paused")
    assert res["success"] is True
    assert res["moved_from"] == "active" and res["moved_to"] == "paused"
    assert res["created_at"] == created_at  # immutable

    # paused -> complete (with a new summary)
    res = await _reg_update(
        workspace_path=str(temp_project_dir), type="update", uuid=uid, status="complete", summary="Done (COMPLETE: 3/3)"
    )
    assert res["success"] is True
    assert res["moved_to"] == "completed"

    registry = parse_registry(str(temp_project_dir))
    assert not [e for e in registry["active"] if e["uuid"] == uid]
    assert not [e for e in registry["paused"] if e["uuid"] == uid]
    row = [e for e in registry["completed"] if e["uuid"] == uid]
    assert row and row[0]["status"] == "✅"
    assert row[0]["created_at"] == created_at  # never updated
    assert row[0]["summary"] == "Done (COMPLETE: 3/3)"
    # Date refreshed on status change (>= created).
    assert row[0]["date"] >= created_at


async def test_reg_update_delete_requires_user_approval(temp_project_dir):
    created = create_registry_entry(str(temp_project_dir), summary="Doomed")
    uid = created["created_uuid"]

    # No confirmation -> server refuses.
    res = await _reg_update(workspace_path=str(temp_project_dir), type="delete", uuid=uid)
    assert res["success"] is False
    assert res.get("needs_approval") is True
    assert any(e["uuid"] == uid for e in parse_registry(str(temp_project_dir))["active"])

    # With confirmation -> deleted.
    res = await _reg_update(workspace_path=str(temp_project_dir), type="delete", uuid=uid, confirmed=True)
    assert res["success"] is True
    assert res["deleted_uuid"] == uid
    registry = parse_registry(str(temp_project_dir))
    assert not any(uid in [e["uuid"] for e in registry[t]] for t in ("active", "paused", "completed"))


async def test_reg_update_unknown_type(temp_project_dir):
    res = await _reg_update(workspace_path=str(temp_project_dir), type="explode")
    assert res["success"] is False
    assert "create, update, or delete" in res["error"]


def test_legacy_4_column_rows_backcompat(temp_project_dir, plan_uuid):
    legacy = (
        "# Active Registry Plan\n\n"
        "| UUID | Status | Date | Summary |\n"
        "|------|--------|------|---------|\n"
        f"| {plan_uuid} | ⏹️ | 2026-08-08 | Legacy |\n"
        "\n# Paused Registry Plan\n\n"
        "| UUID | Status | Date | Summary |\n"
        "|------|--------|------|---------|\n"
        "\n# Completed Registry Plan\n\n"
        "| UUID | Status | Date | Summary |\n"
        "|------|--------|------|---------|\n"
    )
    path: Path = temp_project_dir / ".ai" / "artifacts" / "registry.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file_safe(path, legacy)

    registry = parse_registry(str(temp_project_dir))
    row = registry["active"][0]
    assert row["uuid"] == plan_uuid
    assert row["created_at"] == row["date"] == "2026-08-08"
    assert row["summary"] == "Legacy"
