"""Tests for three-tier orchestration — async bake scheduler + subagent gate (Phase 6).

Three tiers, one store: the inline tick (every ``action_call``), the async background
scheduler (``bake_scheduler``), and the subagent tier (driven by the ``should_spawn_subagent``
gate). All tiers share ``.ai/memory-bank/observations.jsonl`` + ``baked.json`` and the same
``bake_tick``, so candidates must be identical regardless of tier.
"""

from pathlib import Path

from mcp_server.helpers.baking import (
    bake_tick,
    deliver_candidates,
    read_baked,
    should_spawn_subagent,
)
from mcp_server.helpers.observation_store import append_observations
from mcp_server.modules import bake_scheduler


def _seed(workspace: Path) -> None:
    append_observations(
        workspace,
        records=[
            {"signature": "cmd_pnpm", "value": "always use pnpm install", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "Always use pnpm install.", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "always  use pnpm install!", "source": "explicit", "stack": "python"},
            {"signature": "cmd_pnpm", "value": "ALWAYS USE PNPM INSTALL", "source": "explicit", "stack": "python"},
        ],
    )


class TestScheduler:
    def test_note_and_sweep_bakes(self, temp_project_dir: Path):
        _seed(temp_project_dir)
        bake_scheduler.note_workspace(temp_project_dir)
        bake_scheduler.sweep_once()  # async tier, run synchronously for the test
        saved = read_baked(temp_project_dir)
        assert len(saved["candidates"]) == 1
        assert saved["candidates"][0]["signature"] == "cmd_pnpm"

    def test_sweep_ignores_unknown_and_missing(self, temp_project_dir: Path):
        bake_scheduler.sweep_once()  # no known workspaces → no-op, no crash
        bake_scheduler.note_workspace(temp_project_dir / "does-not-exist")
        bake_scheduler.sweep_once()  # missing store → best-effort, no crash

    def test_start_stop_idempotent(self):
        try:
            t = bake_scheduler.start_scheduler(interval=0.2)
            assert t is not None and t.is_alive()
            # Starting again while alive returns the SAME daemon (no duplicate threads).
            assert bake_scheduler.start_scheduler(interval=0.2) is t
        finally:
            bake_scheduler.stop_scheduler()


class TestSubagentGate:
    def test_gate_opens_on_new_candidates(self, temp_project_dir: Path):
        _seed(temp_project_dir)
        bake_tick(temp_project_dir)
        gate = should_spawn_subagent(temp_project_dir)
        assert gate["should_spawn"] is True
        assert gate["undelivered_count"] == 1
        assert gate["candidates"][0]["signature"] == "cmd_pnpm"

    def test_gate_closes_after_delivery(self, temp_project_dir: Path):
        _seed(temp_project_dir)
        bake_tick(temp_project_dir)
        deliver_candidates(temp_project_dir)  # tell-once advances the marker
        gate = should_spawn_subagent(temp_project_dir)
        assert gate["should_spawn"] is False
        assert gate["undelivered_count"] == 0
        assert gate["candidates"] == []

    def test_gate_caps_candidates(self, temp_project_dir: Path):
        # Two distinct emerging candidates → cap at max_candidates=1.
        append_observations(
            temp_project_dir,
            records=[
                {"signature": "a", "value": "always use pnpm", "source": "explicit", "stack": "python"},
                {"signature": "a", "value": "Always use pnpm", "source": "explicit", "stack": "python"},
                {"signature": "a", "value": "always  use pnpm", "source": "explicit", "stack": "python"},
                {"signature": "a", "value": "ALWAYS USE PNPM", "source": "explicit", "stack": "python"},
                {"signature": "b", "value": "prefer dataclasses", "source": "explicit", "stack": "python"},
                {"signature": "b", "value": "Prefer dataclasses", "source": "explicit", "stack": "python"},
                {"signature": "b", "value": "prefer  dataclasses", "source": "explicit", "stack": "python"},
                {"signature": "b", "value": "PREFER DATACLASSES", "source": "explicit", "stack": "python"},
            ],
        )
        bake_tick(temp_project_dir)
        gate = should_spawn_subagent(temp_project_dir, max_candidates=1)
        assert gate["should_spawn"] is True
        assert gate["undelivered_count"] == 2
        assert len(gate["candidates"]) == 1


class TestThreeTierConsistency:
    def test_inline_and_async_produce_identical_candidates(self, temp_project_dir: Path):
        _seed(temp_project_dir)
        inline = bake_tick(temp_project_dir)  # tier: inline (every action_call)
        bake_scheduler.note_workspace(temp_project_dir)
        bake_scheduler.sweep_once()  # tier: async background loop
        async_saved = read_baked(temp_project_dir)
        assert async_saved["candidates"] == inline["candidates"]

    def test_rebake_deterministic_and_no_write_when_unchanged(self, temp_project_dir: Path):
        _seed(temp_project_dir)
        first = bake_tick(temp_project_dir)
        second = bake_tick(temp_project_dir)
        assert first["candidates"] == second["candidates"]
        assert first["changed"] is True
        assert second["changed"] is False  # nothing new → no write
