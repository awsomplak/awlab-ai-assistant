"""
MCP Server — thin entry point.

All tool handlers have been extracted into ``modules/`` for maintainability.
This file exists only to trigger registration and provide backward-compatible access
to the ``mcp`` instance, ``_wrap`` helper, and ``main()`` entry point.
"""

from .modules import registration  # noqa: F401 — triggers @mcp.tool() decorators at import time
from .modules.lifecycle import main

if __name__ == "__main__":
    main()
