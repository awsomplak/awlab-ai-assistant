"""
Tests for helpers/file_utils.py

Covers:
- read_utf8 (read existing file, missing file)
- parse_tasks_md (checklist parsing, headings, status markers)
- update_task_status_in_md (modify a task status in-place)
- write_file_safe (atomic write + cross-process file lock)
"""

from __future__ import annotations

import multiprocessing as _mp
from pathlib import Path

import pytest

from mcp_server.helpers import (
    parse_tasks_md,
    read_utf8,
    update_task_status_in_md,
)

# ── read_utf8 ─────────────────────────────────────────────────────────


class TestReadFileSafe:
    def test_read_existing_file(self, temp_project_dir: str | Path, setup_env_md: str):
        """Should read an existing file successfully."""
        path = Path(setup_env_md)
        content = read_utf8(path)
        assert content is not None
        assert "Windows 11" in content

    def test_read_existing_plan_file(self, temp_project_dir: str | Path, setup_plan_md: str):
        """Should read a plan.md file successfully."""
        path = Path(setup_plan_md)
        content = read_utf8(path)
        assert content is not None
        assert "User Authentication" in content

    def test_read_nonexistent_file(self, temp_project_dir: str | Path):
        """Should handle missing file gracefully."""
        path = (Path(temp_project_dir) if isinstance(temp_project_dir, str) else temp_project_dir) / "nonexistent.md"
        content = read_utf8(path)
        assert content is None

    def test_read_directory(self, temp_project_dir: str | Path):
        """Should return None for a directory."""
        path = Path(temp_project_dir) if isinstance(temp_project_dir, str) else temp_project_dir
        content = read_utf8(path)
        assert content is None


# ── parse_tasks_md ────────────────────────────────────────────────────────


class TestParseTasksMd:
    def test_parse_full_tasks(self, setup_tasks_md: str):
        """Should parse all phases, tasks, and subtasks with correct statuses."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        result = parse_tasks_md(content)
        phases = result["phases"]
        assert len(phases) == 2

        # Phase 1
        assert phases[0]["phase_number"] == 1
        assert len(phases[0]["tasks"]) == 2

        # Task 1
        t1 = phases[0]["tasks"][0]
        assert t1["description"] == "Task 1: Implement JWT authentication"
        assert t1["status"] == "[ ]"
        assert len(t1["subtasks"]) == 2
        assert t1["subtasks"][0]["description"] == "Task 1.1: Create JWT utility"
        assert t1["subtasks"][0]["status"] == "[ ]"

        # Task 2
        t2 = phases[0]["tasks"][1]
        assert t2["description"] == "Task 2: Setup database models"
        assert t2["status"] == "[x]"
        assert t2["subtasks"][1]["status"] == "[x✓]"

        # Phase 2
        assert phases[1]["phase_number"] == 2
        assert len(phases[1]["tasks"]) == 2
        assert phases[1]["tasks"][0]["status"] == "[ ]"
        assert phases[1]["tasks"][1]["status"] == "[x!]"

    def test_empty_tasks(self):
        """Should handle empty task file gracefully."""
        content = "# Tasks\n\n## Phase 1: Test\n"
        result = parse_tasks_md(content)
        assert len(result["phases"]) == 1
        assert result["phases"][0]["tasks"] == []

    def test_no_phases(self):
        """Should handle content with no phases."""
        content = "# Just a heading\n\nSome text\n"
        result = parse_tasks_md(content)
        assert result["phases"] == []

    def test_empty_content(self):
        """Should handle empty content."""
        result = parse_tasks_md("")
        assert result["phases"] == []


# ── update_task_status_in_md ──────────────────────────────────────────────


class TestUpdateTaskStatusInMd:
    def test_update_toplevel_task(self, setup_tasks_md: str):
        """Should update a top-level task status marker."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        updated = update_task_status_in_md(content, "1.1", "[x]")
        assert updated is not None

        # Re-parse and verify
        parsed = parse_tasks_md(updated)
        assert parsed["phases"][0]["tasks"][0]["status"] == "[x]"

    def test_update_phase2_task(self, setup_tasks_md: str):
        """Should update tasks in Phase 2."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        updated = update_task_status_in_md(content, "2.2", "[x]")
        assert updated is not None

        parsed = parse_tasks_md(updated)
        assert parsed["phases"][1]["tasks"][1]["status"] == "[x]"

    def test_nonexistent_phase(self, setup_tasks_md: str):
        """Should return None for out-of-range phase."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        result = update_task_status_in_md(content, "99.1", "[x]")
        assert result is None

    def test_nonexistent_task(self, setup_tasks_md: str):
        """Should return None for out-of-range task."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        result = update_task_status_in_md(content, "1.99", "[x]")
        assert result is None

    def test_invalid_task_path_format(self, setup_tasks_md: str):
        """Should reject malformed task path."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        result = update_task_status_in_md(content, "abc", "[x]")
        assert result is None

    def test_deferred_status(self, setup_tasks_md: str):
        """Should support ⏳ status marker."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        updated = update_task_status_in_md(content, "1.2", "[⏳]")
        assert updated is not None

        parsed = parse_tasks_md(updated)
        assert parsed["phases"][0]["tasks"][1]["status"] == "[⏳]"

    def test_preserve_file_structure(self, setup_tasks_md: str):
        """Should preserve the rest of the file when updating."""
        content = Path(setup_tasks_md).read_text(encoding="utf-8")
        updated = update_task_status_in_md(content, "1.1", "[x✓]")
        assert updated is not None

        # Check that Phase 2 content is still preserved
        assert "Phase 2: Frontend Auth" in updated
        assert "Task 3: Login page" in updated


# ── Task 3.2: cross-process file-lock race (G4) ─────────────────────────────
#
# Two child processes target the same path via write_file_safe simultaneously.
# The flock (POSIX) or sentinel-file (Windows) must serialize them so the final
# file content is exactly one of the two payloads — never a mix, never empty,
# never a half-write. Without the lock, the atomic-replace pattern alone is
# not enough if two processes both think they own the write.


def _race_worker(path_str: str, payload: str, ready_evt, go_evt, done_evt) -> None:
    """Subprocess worker: block on ``go_evt``, then write ``payload`` to ``path``.

    Run via ``multiprocessing.get_context("spawn")`` so each child is a fresh
    process that imports this module — the lock can therefore be observed
    across processes (in-process locks like ``threading.Lock`` would NOT be
    sufficient). We sync the two children on a barrier so they hit the lock
    as close to simultaneously as possible.
    """
    from mcp_server.helpers.file_utils import write_file_safe

    ready_evt.set()       # tell parent we're initialized
    go_evt.wait()         # wait for the green light
    # Tiny sleep inside the critical section would expose the race most
    # clearly, but the lock must hold regardless — write_file_safe blocks
    # the second child until the first releases.
    assert write_file_safe(Path(path_str), payload), f"child failed to write {path_str}"
    done_evt.set()


def test_two_processes_writing_same_path_do_not_corrupt(tmp_path: Path) -> None:
    """Two child processes call write_file_safe on the same path back-to-back.

    The flock (POSIX) / sentinel-file (Windows) must serialize them so the
    final content is exactly one of the two payloads — proving no torn write
    and no mixed bytes.
    """
    target = tmp_path / "tasks.md"
    payload_a = "A" * 200 + "_marker_A"     # long enough to span multiple write() syscalls
    payload_b = "B" * 200 + "_marker_B"

    ctx = _mp.get_context("spawn")
    # 2 events per process: ready, go, done — used as a barrier so both
    # children hit write_file_safe as close to simultaneously as possible.
    ready_a, ready_b = ctx.Event(), ctx.Event()
    go = ctx.Event()
    done_a, done_b = ctx.Event(), ctx.Event()

    proc_a = ctx.Process(
        target=_race_worker, args=(str(target), payload_a, ready_a, go, done_a)
    )
    proc_b = ctx.Process(
        target=_race_worker, args=(str(target), payload_b, ready_b, go, done_b)
    )
    proc_a.start()
    proc_b.start()
    # Wait for both children to be initialized
    assert ready_a.wait(timeout=10), "child A never became ready"
    assert ready_b.wait(timeout=10), "child B never became ready"
    # Release them simultaneously
    go.set()
    # Wait for both to complete
    assert done_a.wait(timeout=10), f"child A timed out (exit={proc_a.exitcode})"
    assert done_b.wait(timeout=10), f"child B timed out (exit={proc_b.exitcode})"
    proc_a.join(timeout=5)
    proc_b.join(timeout=5)

    # Final file must exist (both children reported success) and contain
    # EXACTLY one of the two payloads — no mixing, no truncation.
    assert target.exists(), "target file was never written"
    final = target.read_text(encoding="utf-8")
    assert final in (payload_a, payload_b), (
        f"final content is neither payload A nor payload B (corruption): "
        f"len={len(final)}, head={final[:60]!r}, tail={final[-60:]!r}"
    )
    # Lock sentinel must have been cleaned up (write_file_safe releases
    # the fd / unlinks the sentinel on every exit path).
    assert not target.with_suffix(target.suffix + ".lock").exists(), (
        "lock sentinel was not cleaned up after the race"
    )
