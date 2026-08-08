"""Offline cache (pending.jsonl) tests — server-side queueing + mem_replay replay.

Covers rule 14-mcp-offline-cache: mutations are queued (NEVER dropped) when a
store/DB write fails, and ``mem_replay`` drains the queue keeping failures.
"""

from mcp_server.registry import _mem_replay, _mem_write
from mcp_server.tools.plan_tools import update_task_status
from mcp_server.tools.plan_tools.io import (
    append_pending,
    clear_pending,
    pending_path,
    read_pending,
    replace_pending,
)

# ── Queue helpers ──────────────────────────────────────────────────────────


def test_pending_roundtrip_and_corrupt_line(tmp_path):
    assert append_pending(tmp_path, {"type": "mem_write", "store": "project"}) is True
    assert append_pending(tmp_path, {"type": "mem_remove"}) is True
    # A torn/corrupt tail line must be skipped, not crash the reader.
    with open(pending_path(tmp_path), "a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    assert read_pending(tmp_path) == [
        {"type": "mem_write", "store": "project"},
        {"type": "mem_remove"},
    ]


def test_replace_and_clear_pending(tmp_path):
    append_pending(tmp_path, {"type": "a"})
    append_pending(tmp_path, {"type": "b"})
    assert replace_pending(tmp_path, [{"type": "b"}]) is True
    assert read_pending(tmp_path) == [{"type": "b"}]
    assert clear_pending(tmp_path) is True
    assert not pending_path(tmp_path).exists()


# ── Server-side queueing on store failure ──────────────────────────────────


async def test_task_update_queues_sync_on_db_failure(
    temp_project_dir, project_id, setup_tasks_md, plan_uuid, monkeypatch
):
    """DB sync down → task progress is queued (sync_plan_progress), not lost."""
    import mcp_server.tools.plan_tools.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "sync_to_agent_recall", lambda *a, **k: False)

    result = await update_task_status(
        workspace_path=temp_project_dir,
        project_id=project_id,
        plan_uuid=plan_uuid,
        task_path="1.1",
        new_status="[x]",
    )
    assert result["success"] is True
    assert result["db_synced"] is False
    assert result["pending_queued"] is True

    entries = read_pending(temp_project_dir)
    assert any(e.get("type") == "sync_plan_progress" and e.get("plan_uuid") == plan_uuid for e in entries)


async def test_mem_write_queues_on_store_failure(temp_project_dir, monkeypatch):
    """Store write raises → the full mem_write is queued, never dropped."""
    import mcp_server.helpers as helpers_mod

    def _boom(*a, **k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(helpers_mod, "add_observations", _boom)

    result = await _mem_write(
        workspace_path=str(temp_project_dir),
        observations=[{"entityName": "Foo", "contents": ["must-not-lose"]}],
    )
    assert result["success"] is False
    assert result["queued"] is True

    entries = read_pending(temp_project_dir)
    assert entries and entries[-1]["type"] == "mem_write"
    assert entries[-1]["observations"] == [{"entityName": "Foo", "contents": ["must-not-lose"]}]


# ── mem_replay ─────────────────────────────────────────────────────────────


async def test_mem_replay_drains_and_keeps_failed(temp_project_dir):
    append_pending(
        temp_project_dir,
        {
            "type": "mem_write",
            "workspace_path": str(temp_project_dir),
            "store": "project",
            "observations": [{"entityName": "ReplayFoo", "contents": ["replayed"]}],
        },
    )
    append_pending(temp_project_dir, {"type": "totally_unknown"})

    res = await _mem_replay(workspace_path=str(temp_project_dir))
    assert res["success"] is True
    assert res["processed"] == 1
    assert any(s["type"] == "mem_write" for s in res["succeeded"])
    assert any(f["type"] == "totally_unknown" for f in res["failed"])
    assert res["pending_left"] == 1
    # The failed entry stays queued for a later retry.
    assert [e["type"] for e in read_pending(temp_project_dir)] == ["totally_unknown"]


async def test_mem_replay_dry_run_previews(tmp_path):
    append_pending(tmp_path, {"type": "mem_write"})
    res = await _mem_replay(workspace_path=str(tmp_path), dry_run=True)
    assert res["success"] is True
    assert res["dry_run"] is True
    assert res["count"] == 1
    assert res["pending"] == [{"type": "mem_write"}]
    # Nothing applied; queue untouched.
    assert read_pending(tmp_path) == [{"type": "mem_write"}]
