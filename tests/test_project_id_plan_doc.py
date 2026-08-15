"""
Project identity + plan/notes doc actions — Phase 3.

Locks in:
1. project_id check-and-create — missing → auto-generates (sanitized dir-name slug);
   exists → no-op; idempotent; prevents global-DB fallback.
2. plan_doc read/write/delete for plan.md AND notes.md (full content, no template).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.registry import _plan_doc, _project_id_check

# ── project_id check-and-create ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_id_missing_creates(tmp_path: Path):
    r = await _project_id_check(str(tmp_path))
    assert r["success"]
    assert r["created"] is True
    assert r["action"] == "create"
    pid_file = tmp_path / ".ai" / "project-id"
    assert pid_file.exists()
    assert r["project_id"] == pid_file.read_text(encoding="utf-8").strip()


@pytest.mark.asyncio
async def test_project_id_exists_is_noop(tmp_path: Path):
    # Pre-create a custom project-id
    pid_dir = tmp_path / ".ai"
    pid_dir.mkdir(exist_ok=True)
    (pid_dir / "project-id").write_text("my-custom-id", encoding="utf-8")

    r = await _project_id_check(str(tmp_path))
    assert r["success"]
    assert r["created"] is False
    assert r["action"] == "check"
    assert r["project_id"] == "my-custom-id"  # existing id preserved, not overwritten


@pytest.mark.asyncio
async def test_project_id_force_regenerate(tmp_path: Path):
    pid_dir = tmp_path / ".ai"
    pid_dir.mkdir(exist_ok=True)
    (pid_dir / "project-id").write_text("old-id", encoding="utf-8")

    r = await _project_id_check(str(tmp_path), force_regenerate=True)
    assert r["success"]
    assert r["created"] is True
    # regenerated slug differs from the custom old-id
    assert r["project_id"] != "old-id"
    assert r["project_id"] == pid_dir.joinpath("project-id").read_text(encoding="utf-8").strip()


@pytest.mark.asyncio
async def test_project_id_slug_is_sanitized(tmp_path: Path):
    weird = tmp_path / "My Project! (x)"
    weird.mkdir(parents=True)
    r = await _project_id_check(str(weird))
    assert r["success"]
    assert "_" in r["project_id"] or r["project_id"].islower()  # sanitized, lowercased


# ── plan_doc read/write/delete ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_doc_write_and_read_plan(tmp_path: Path):
    content = "# Plan X\n\n## Overview\n\nDo the thing.\n"
    w = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="write", content=content)
    assert w["success"] and w["mode"] == "write"

    rd = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="read")
    assert rd["success"] and rd["content"] == content


@pytest.mark.asyncio
async def test_plan_doc_write_and_read_notes(tmp_path: Path):
    content = "# Notes\n\n## Constraints\n\n- never rewrite in place\n"
    w = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="notes", mode="write", content=content)
    assert w["success"]

    rd = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="notes", mode="read")
    assert rd["success"] and rd["content"] == content


@pytest.mark.asyncio
async def test_plan_doc_delete(tmp_path: Path):
    await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="write", content="# P\n")
    d = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="delete")
    assert d["success"] and d["mode"] == "delete"
    assert not (tmp_path / ".ai" / "artifacts" / "ab12cd34" / "plan.md").exists()


@pytest.mark.asyncio
async def test_plan_doc_read_missing_errors(tmp_path: Path):
    rd = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="read")
    assert not rd["success"]
    assert "not found" in rd["error"]


@pytest.mark.asyncio
async def test_plan_doc_invalid_doc_or_mode(tmp_path: Path):
    assert not (await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="bogus", mode="read"))["success"]
    assert not (await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="nope"))["success"]


@pytest.mark.asyncio
async def test_plan_doc_write_requires_content(tmp_path: Path):
    r = await _plan_doc(str(tmp_path), plan_uuid="ab12cd34", doc="plan", mode="write", content=None)
    assert not r["success"]
    assert "content required" in r["error"]
