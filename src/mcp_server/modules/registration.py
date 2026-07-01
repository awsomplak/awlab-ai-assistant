"""
Tool registration for awlab-mcp server — utility, context, and file tools.

Imports the `mcp` instance from .lifecycle and registers only
utility + context + file tools. Plan and memory tools moved to
registration_plan.py and registration_memory.py respectively.
"""

import json
from typing import Annotated

from pydantic import Field
from .lifecycle import mcp
from ..helpers.logger import logger

# ── Import business-logic implementations ─────────────────────────────────

from ..tools.file_tools import read_memory_bank as _read_memory_bank
from ..tools.context_tools import (
    get_context_snapshot as _get_context_snapshot,
    scan_project as _scan_project,
    suggest_relevant_files as _suggest_relevant_files,
)
from ..tools.utils_tools import (
    get_server_version as _get_server_version,
    get_environment as _get_environment,
)
from ..helpers import ok_json as _ok, fail_json as _fail


# ══════════════════════════════════════════════════════════════════════════
# ── File Tools ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="ctx_read_memory_bank")
async def read_memory_bank(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    filename: Annotated[str, Field(description="File to read (only environment.md)")]
) -> str:
    """
    Read allowed file from .ai/memory-bank/.
    """
    result = await _read_memory_bank(workspace_path=workspace_path, filename=filename)
    return json.dumps(result)


# ══════════════════════════════════════════════════════════════════════════
# ── Context & Intelligence Tools ────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="ctx_get_snapshot")
async def get_context_snapshot(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")]
) -> str:
    """
    Get active plan, patterns, project ID.
    """
    result = await _get_context_snapshot(workspace_path=workspace_path)
    return json.dumps(result)


@mcp.tool(name="ctx_suggest_files")
async def suggest_relevant_files(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")],
    task_description: Annotated[str, Field(description="Task description for file suggestions")]
) -> str:
    """
    Suggest files for a task.
    """
    result = await _suggest_relevant_files(workspace_path, task_description)
    return json.dumps(result)


@mcp.tool(name="ctx_scan_project")
async def scan_project(
    workspace_path: Annotated[str, Field(description="Absolute path to the project workspace root")] = "",
    force_refresh: Annotated[bool, Field(description="Bypass cache, force fresh scan")] = False
) -> str:
    """
    Scan project framework & entry points.
    """
    result = await _scan_project(workspace_path=workspace_path, force_refresh=force_refresh)
    return json.dumps(result)


# ══════════════════════════════════════════════════════════════════════════
# ── Utility Tools ───────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════


@mcp.tool(name="util_get_version")
async def get_server_version() -> str:
    """
    Return server version and build tag. No params needed.
    """
    result = await _get_server_version()
    return json.dumps(result)


@mcp.tool(name="util_get_project_meta")
async def get_project_meta() -> str:
    """
    Return project build metadata. No params needed.
    """
    result = await _get_environment()
    return json.dumps(result)
