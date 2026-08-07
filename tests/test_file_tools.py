"""
Tests for tools/file_tools.py

Covers:
- read_memory_bank: allowed file (environment.md), disallowed file, missing file
"""

from pathlib import Path

from mcp_server.tools.file_tools import read_memory_bank


class TestReadMemoryBank:
    async def test_read_allowed_file(self, temp_project_dir: str | Path, setup_env_md: str):
        """Should read environment.md successfully."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename="environment.md")
        assert result["success"] is True
        assert "Windows 11" in result["content"]

    async def test_read_disallowed_file(self, temp_project_dir: str | Path, setup_plan_md: str):
        """Should reject filenames not in the allowlist."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename="plan.md")
        assert result["success"] is False
        assert "error" in result

    async def test_read_nonexistent_file(self, temp_project_dir: str | Path):
        """Should return error for file that doesn't exist."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename="environment.md")
        assert result["success"] is False
        assert "error" in result

    async def test_path_traversal_attempt(self, temp_project_dir: str | Path):
        """Should block path traversal attempts."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename="../config.py")
        assert result["success"] is False

    async def test_empty_filename(self, temp_project_dir: str | Path):
        """Should reject empty filename."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename="")
        assert result["success"] is False

    async def test_none_filename(self, temp_project_dir: str | Path):
        """Should reject None filename."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename=None)
        assert result["success"] is False

    async def test_read_with_content(self, temp_project_dir: str | Path, setup_env_md: str):
        """Should return non-empty content."""
        result = await read_memory_bank(workspace_path=temp_project_dir, filename="environment.md")
        assert len(result["content"]) > 0
