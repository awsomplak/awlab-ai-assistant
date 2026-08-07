"""
PyInstaller entry point — uses absolute imports so the frozen executable works.

Usage:
    python -m mcp_server          # dev mode (same as server.py)
    pyinstaller --onefile __main__.py  # → dist executable
"""

import multiprocessing
import sys
from pathlib import Path

# Allow ProcessPoolExecutor (graphify parallel extraction) inside the frozen
# exe: worker children re-execute the binary and freeze_support() routes them
# into the pool bootstrap BEFORE the app imports / server start below.
multiprocessing.freeze_support()

# Ensure the parent of mcp_server/ is on sys.path so absolute imports resolve
_src = Path(__file__).resolve().parent.parent  # src/
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mcp_server.modules import registration  # noqa: E402, F401 — triggers @mcp.tool() decorators at import time
from mcp_server.modules.lifecycle import main  # noqa: E402

if __name__ == "__main__":
    main()
