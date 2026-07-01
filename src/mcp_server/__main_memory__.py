"""
PyInstaller entry point for awlab-memory server.

Memory/knowledge-graph tools — intended as a separate MCP server
to work around Copilot per-server tool limits.
"""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent  # src/
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mcp_server.modules.lifecycle import run_server  # noqa: E402
from mcp_server.modules import registration_memory  # noqa: F401 — triggers @mcp.tool() decorators
from mcp_server.modules.registration_memory import mcp  # noqa: E402

run_server(mcp, server_name="awlab-memory")
