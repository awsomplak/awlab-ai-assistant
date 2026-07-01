"""
PyInstaller entry point — uses absolute imports so the frozen executable works.

Usage:
    python -m mcp_server          # dev mode (same as server.py)
    pyinstaller --onefile __main__.py  # → dist executable
"""

import sys
from pathlib import Path

# Ensure the parent of mcp_server/ is on sys.path so absolute imports resolve
_src = Path(__file__).resolve().parent.parent  # src/
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mcp_server.modules.lifecycle import mcp, main  # noqa: E402
from mcp_server.modules import registration  # noqa: F401 — triggers @mcp.tool() decorators at import time

main()
