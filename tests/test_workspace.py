"""Tests for the workspace resolver module.

Tests the simplified workspace module that provides:
- is_valid_project_root: validates project directory markers
- resolve_db_path: resolves agent-recall database directory
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_server.config import settings
from mcp_server.helpers.validation import validate_project_root
from mcp_server.helpers.workspace import resolve_db_path

# ── validate_project_root tests ────────────────────────────────────────────


class TestIsValidProjectRoot:
    # Known project-root markers (file names / directory names). Compacted into
    # one loop so every marker is still asserted in isolation while keeping the
    # suite lean.
    _FILE_MARKERS = [
        "pyproject.toml",
        "setup.py",
        ".gitignore",
        "package.json",
        "requirements.txt",
        "go.mod",
        "Cargo.toml",
        "composer.json",
        "Gemfile",
        "manage.py",
        "artisan",
        "next.config.js",
        "next.config.ts",
        "setup.cfg",
    ]
    _DIR_MARKERS = [".git"]

    def test_detects_project_markers(self, tmp_path):
        """Every known project-root marker is detected (each in isolation)."""
        for marker in self._FILE_MARKERS:
            (tmp_path / marker).write_text("")
            assert validate_project_root(str(tmp_path)) is True, f"marker {marker} not detected"
            (tmp_path / marker).unlink()
        for marker in self._DIR_MARKERS:
            (tmp_path / marker).mkdir()
            assert validate_project_root(str(tmp_path)) is True, f"marker {marker} not detected"
            (tmp_path / marker).rmdir()

    def test_returns_false_without_project_markers(self, tmp_path):
        """No markers, a nonexistent path, or a file path all return False."""
        assert validate_project_root(str(tmp_path)) is False  # empty dir
        assert validate_project_root("/nonexistent/path") is False  # missing path
        f = tmp_path / "some_file.txt"
        f.write_text("")
        assert validate_project_root(str(f)) is False  # file, not a directory


# ── resolve_db_path tests ──────────────────────────────────────────────────


class TestResolveDbHome:
    @pytest.fixture(autouse=True)
    def _clear_db_path_cache(self):
        """Clear the cached settings.db_path before each test."""
        settings.__dict__.pop("db_path", None)
        yield

    def test_db_path_env_var_takes_priority(self, tmp_path):
        """DB_PATH env var should be used regardless of workspace_path."""
        custom_path = str(tmp_path / "custom-db")
        with patch.dict(os.environ, {"DB_PATH": custom_path}, clear=False):
            result = resolve_db_path(workspace_path="/some/project")
            assert str(result) == str(Path(os.path.abspath(custom_path)) / "memory.db")

    def test_project_id_uses_isolated_path(self, tmp_path):
        """project_id param should use project-isolated DB path."""
        result = resolve_db_path(workspace_path=str(tmp_path), project_id="test-project")
        expected = str(tmp_path / ".ai" / "memory-bank" / "memory" / "memory_test-project.db")
        assert str(result) == expected

    def test_project_id_file_uses_isolated_path(self, tmp_path):
        """Existing .ai/project-id file should use project-isolated DB path."""
        project_id_file = tmp_path / ".ai" / "project-id"
        project_id_file.parent.mkdir(parents=True, exist_ok=True)
        project_id_file.write_text("my-project-id")
        result = resolve_db_path(workspace_path=str(tmp_path))
        expected = str(tmp_path / ".ai" / "memory-bank" / "memory" / "memory_my-project-id.db")
        assert str(result) == expected

    def test_fallback_to_user_wide_dir(self, tmp_path):
        """No project-id and no DB_PATH should fallback to ~/.awlab-id/agent-memory/memory/memory.db."""
        result = resolve_db_path(workspace_path=str(tmp_path))
        expected = str(Path.home() / ".awlab-id" / "agent-memory" / "memory" / "memory.db")
        assert str(result) == expected
