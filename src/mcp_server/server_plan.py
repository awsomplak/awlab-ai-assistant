"""
Console-script entry point for awlab-plan server.

Usage:
    awlab-plan
    python -m mcp_server.server_plan
"""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent  # src/
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mcp_server.modules.lifecycle import run_server
from mcp_server.modules import registration_plan  # noqa: F401
from mcp_server.modules.registration_plan import mcp


def main():
    """Entry point for awlab-plan MCP server."""
    run_server(mcp, server_name="awlab-plan")


if __name__ == "__main__":
    main()
