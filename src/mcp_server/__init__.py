"""
mcp_server — Project-isolated MCP server for cline-ai-assisted-dev.

Provides tools for:
- Plan & Task Management
- Agent-Recall Memory Graph (via agent_recall pip package)
- File & Registry Utilities
- Logging with daily rotation (helpers/logger.py)
"""

from .helpers.logger import logger

__all__ = [
    "logger",
]
