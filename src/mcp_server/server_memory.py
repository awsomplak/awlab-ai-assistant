"""
Console-script entry point for awlab-memory server.

Usage:
    awlab-memory
    python -m mcp_server.server_memory
"""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent  # src/
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mcp_server.modules.lifecycle import run_server
from mcp_server.modules import registration_memory  # noqa: F401
from mcp_server.modules.registration_memory import mcp


def main():
    """Entry point for awlab-memory MCP server."""
    run_server(mcp, server_name="awlab-memory")


if __name__ == "__main__":
    main()
