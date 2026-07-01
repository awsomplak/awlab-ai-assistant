"""
Utility Tools.

Tools:
- generate_mermaid: Generate a Mermaid flowchart from phases/dependencies
- format_tasks_as_markdown: Format a structured task list as markdown
- get_environment: Return OS, shell, working directory info
"""

import os
import sys
import platform
from typing import Any
from ..config import settings
from ..helpers import (
    read_utf8,
    resp_obj,
    fail_obj,
    parse_tasks_md,
)
from .._version import VERSION_STRING

async def generate_mermaid(
    phases: list[str] | None,
    dependencies: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Generate a Mermaid flowchart from a list of phases and optional dependencies.

    Args:
        phases: List of phase names (e.g., ["Backend Auth", "Frontend Auth"]).
        dependencies: List of { "from": str, "to": str } edges.

    Returns:
        { "mermaid_code": "graph TD\\n  A[...] ..." }
    """
    if phases is None:
        return fail_obj(error="phase cannot be None")

    lines: list[str] = ["graph TD"]

    # Create nodes (use P1, P2, ... to support unlimited phases)
    node_ids: list[str] = []
    for i, phase in enumerate(phases):
        node_id = f"P{i + 1}"
        node_ids.append(node_id)
        safe_phase = phase.replace('"', "'")
        lines.append(f"  {node_id}[\"{safe_phase}\"]")

    # Add edges
    if dependencies:
        for dep in dependencies:
            from_id = dep.get("from", "")
            to_id = dep.get("to", "")
            if from_id in node_ids and to_id in node_ids:
                lines.append(f"  {from_id} --> {to_id}")
    else:
        # Default: linear chain
        for i in range(len(node_ids) - 1):
            lines.append(f"  {node_ids[i]} --> {node_ids[i + 1]}")

    return resp_obj(mermaid_code="\n".join(lines))


async def format_tasks_as_markdown(
    workspace_path: str | None = None,
    plan_uuid: str | None = None,
    phases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Format a structured task list as markdown, optionally loading from a plan.

    Args:
        plan_uuid: If provided, load tasks from this plan's tasks.md.
        phases: If provided (and plan_uuid is None), use these phases directly.
        workspace_path: Required when plan_uuid is provided.

    Returns:
        { "markdown": "..." }
    """
    if plan_uuid:
        if not workspace_path:
            return {"success": False, "error": "workspace_path is required when plan_uuid is provided"}
        # Read and parse tasks.md directly using workspace_path
        tasks_path = settings.get_plan_tasks_path(workspace_path=workspace_path, plan_uuid=plan_uuid)
        content = read_utf8(tasks_path)
        if content is None:
            return fail_obj(error=f"tasks.md not found for plan '{plan_uuid}' at {tasks_path}")
        try:
            parsed = parse_tasks_md(content)
            phases = parsed.get("phases", [])
        except Exception as e:
            return fail_obj(error=f"Failed to parse tasks.md: {e}")
    elif phases is None:
        return fail_obj(error="Either plan_uuid or phases must be provided")

    # Guard: ensure phases is iterable (Pylance type-narrowing safeguard)
    if not isinstance(phases, list):
        return fail_obj(error="Internal error: phases must be a list")

    lines: list[str] = ["# Tasks", ""]
    for phase in phases:
        phase_name = phase.get("name", f"Phase {phase.get('phase_number', '?')}")
        lines.append(f"## {phase_name}")
        lines.append("")
        for task in phase.get("tasks", []):
            status = task.get("status", "[ ]")
            desc = task.get("description", "")
            lines.append(f"- {status} {desc}")
            for subtask in task.get("subtasks", []):
                sub_status = subtask.get("status", "[ ]")
                sub_desc = subtask.get("description", "")
                lines.append(f"    - {sub_status} {sub_desc}")
        lines.append("")

    return resp_obj(markdown="\n".join(lines))


async def get_server_version() -> dict[str, Any]:
    """
    Return the server build version string.

    Returns:
        { "version": "awlab-mcp-server v1.1.0+build.001" }
    """
    return resp_obj(version=VERSION_STRING)


async def get_environment() -> dict[str, Any]:
    """
    Return OS, shell, server version, and working directory information.

    Returns:
        { "os": "...", "shell": "...", "server_version": "...", "cwd": "..." }
    """
    shell: str | None = os.environ.get("SHELL")
    if not shell:
        # Windows fallback
        shell = os.environ.get("ComSpec")
    if not shell:
        shell = os.environ.get("COMSPEC")

    environment = {
        "os": sys.platform,
        "os_name": os.name,
        "platform": platform.platform(),
        "shell": shell or "",
        "server_version": VERSION_STRING,
        "cwd": os.getcwd(),
    }
    return resp_obj(**environment)
