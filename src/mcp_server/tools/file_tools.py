"""
File tools — safe file reading from .ai/memory-bank/ directory.
"""

from pathlib import Path
from typing import Any

from ..config import settings


async def read_memory_bank(
    workspace_path: str | Path = "",
    filename: str = "",
) -> dict[str, Any]:
    """
    Safely read an allowed file from the .ai/memory-bank/ directory.

    Allowed files: environment.md (static env config), context.md (orchestration
    state).

    Args:
        workspace_path: Absolute path to the project workspace root.
        filename: Name of the file to read (e.g., 'environment.md').

    Returns:
        { success: bool, content: str } or { success: false, error: str }
    """
    allowed = {"environment.md", "context.md"}
    if filename not in allowed:
        return {"success": False, "error": f"File '{filename}' is not allowed. Allowed: {', '.join(sorted(allowed))}"}

    path = settings.get_memory_bank_dir(workspace_path=workspace_path) / filename
    if not path.exists():
        return {"success": False, "error": f"File not found: {path}"}

    try:
        content = path.read_text(encoding="utf-8")
        return {"success": True, "content": content, "file": filename}
    except OSError as e:
        return {"success": False, "error": str(e)}
