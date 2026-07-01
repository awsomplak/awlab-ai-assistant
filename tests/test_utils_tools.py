"""
Tests for tools/utils_tools.py

Covers:
- generate_mermaid: basic flow, empty phases, with dependencies
- get_environment: returns expected keys
- format_tasks_as_markdown: converts structured tasks to markdown
"""

from pathlib import Path
from mcp_server.tools.utils_tools import (
    generate_mermaid,
    get_environment,
    format_tasks_as_markdown,
)


class TestGenerateMermaid:
    async def test_basic_flow(self):
        """Should generate a simple linear flow."""
        result = await generate_mermaid(["Backend", "Frontend", "Deploy"])
        code = result["mermaid_code"]
        assert "graph TD" in code
        assert "Backend" in code
        assert "Frontend" in code
        assert "Deploy" in code
        # Check arrows exist between consecutive phases
        assert "P1" in code and "P2" in code and "P3" in code
        assert "P1 -->" in code

    async def test_with_dependencies(self):
        """Should add dependency arrows."""
        result = await generate_mermaid(
            phases=["Backend", "Frontend", "Testing"],
            dependencies=[{"from": "P1", "to": "P3"}]
        )
        code = result["mermaid_code"]
        assert "graph TD" in code
        assert "P1 --> P3" in code

    async def test_single_phase(self):
        """Should handle a single phase."""
        result = await generate_mermaid(["Init"])
        code = result["mermaid_code"]
        assert "graph TD" in code
        assert 'P1["Init"]' in code or "P1[Init]" in code

    async def test_empty_phases(self):
        """Should handle empty phases list."""
        result = await generate_mermaid([])
        code = result["mermaid_code"]
        assert "graph TD" in code

    async def test_phases_none(self):
        """Should handle None phases."""
        result = await generate_mermaid(None)
        assert "error" in result


class TestGetEnvironment:
    async def test_returns_expected_keys(self):
        """Should return os, shell, and cwd keys."""
        result = await get_environment()
        assert "os" in result
        assert "shell" in result
        assert "cwd" in result
        assert isinstance(result["os"], str)
        assert isinstance(result["shell"], str)
        assert isinstance(result["cwd"], str)

    async def test_cwd_exists(self):
        """cwd should point to an existing directory."""
        result = await get_environment()
        assert Path(result["cwd"]).exists()


class TestFormatTasksAsMarkdown:
    async def test_basic_format(self):
        """Should format structured task data into markdown."""
        phases = [
            {
                "name": "Phase 1: Backend",
                "phase_number": 1,
                "tasks": [
                    {"description": "Setup DB", "status": "[x]", "subtasks": []},
                    {"description": "Create API", "status": "[ ]", "subtasks": [
                        {"description": "Route", "status": "[ ]", "subtasks": []}
                    ]}
                ]
            }
        ]
        result = await format_tasks_as_markdown(phases=phases)
        assert "## Phase 1: Backend" in result["markdown"]
        assert "[x] Setup DB" in result["markdown"]
        assert "[ ] Create API" in result["markdown"]
        assert "- [ ] Route" in result["markdown"]

    async def test_empty_phases(self):
        """Should handle empty phase list."""
        result = await format_tasks_as_markdown(phases=[])
        assert "# Tasks" in result["markdown"]

    async def test_without_params(self):
        """Should return error when neither plan_uuid nor phases provided."""
        result = await format_tasks_as_markdown()
        assert "error" in result

    async def test_with_plan_uuid(self, temp_project_dir: str, setup_tasks_md: str, plan_uuid: str):
        """Should load tasks from a plan UUID."""
        result = await format_tasks_as_markdown(plan_uuid=plan_uuid, workspace_path=temp_project_dir)
        assert "markdown" in result
        assert "Phase 1: Backend Auth" in result["markdown"]
        assert "Phase 2: Frontend Auth" in result["markdown"]