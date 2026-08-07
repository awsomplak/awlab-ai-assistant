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
    def test_detects_pyproject_toml(self, tmp_path):
        """Should detect pyproject.toml as a project root marker."""
        (tmp_path / "pyproject.toml").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_setup_py(self, tmp_path):
        """Should detect setup.py as a project root marker."""
        (tmp_path / "setup.py").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_git_dir(self, tmp_path):
        """Should detect .git directory as a project root marker."""
        (tmp_path / ".git").mkdir()
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_gitignore(self, tmp_path):
        """Should detect .gitignore as a project root marker."""
        (tmp_path / ".gitignore").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_package_json(self, tmp_path):
        """Should detect package.json as a project root marker."""
        (tmp_path / "package.json").write_text("{}")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_requirements_txt(self, tmp_path):
        """Should detect requirements.txt as a project root marker."""
        (tmp_path / "requirements.txt").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_go_mod(self, tmp_path):
        """Should detect go.mod as a project root marker."""
        (tmp_path / "go.mod").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_cargo_toml(self, tmp_path):
        """Should detect Cargo.toml as a project root marker."""
        (tmp_path / "Cargo.toml").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_composer_json(self, tmp_path):
        """Should detect composer.json as a project root marker."""
        (tmp_path / "composer.json").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_gemfile(self, tmp_path):
        """Should detect Gemfile as a project root marker."""
        (tmp_path / "Gemfile").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_manage_py(self, tmp_path):
        """Should detect manage.py as a project root marker."""
        (tmp_path / "manage.py").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_artisan(self, tmp_path):
        """Should detect artisan as a project root marker."""
        (tmp_path / "artisan").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_next_config_js(self, tmp_path):
        """Should detect next.config.js as a project root marker."""
        (tmp_path / "next.config.js").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_detects_next_config_ts(self, tmp_path):
        """Should detect next.config.ts as a project root marker."""
        (tmp_path / "next.config.ts").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_setup_cfg_detected(self, tmp_path):
        """Should detect setup.cfg as a project root marker."""
        (tmp_path / "setup.cfg").write_text("")
        assert validate_project_root(str(tmp_path)) is True

    def test_returns_false_for_empty_dir(self, tmp_path):
        """Should return False for a directory with no markers."""
        assert validate_project_root(str(tmp_path)) is False

    def test_returns_false_for_nonexistent_path(self):
        """Should return False for a path that doesn't exist."""
        assert validate_project_root("/nonexistent/path") is False

    def test_returns_false_for_file_path(self, tmp_path):
        """Should return False when path points to a file, not a directory."""
        f = tmp_path / "some_file.txt"
        f.write_text("")
        assert validate_project_root(str(f)) is False


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
