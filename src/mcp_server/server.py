"""
MCP Server — thin entry point.

All tool handlers have been extracted into ``modules/`` for maintainability.
This file exists only to trigger registration and provide backward-compatible access
to the ``mcp`` instance, ``_wrap`` helper, and ``main()`` entry point.
"""

from .modules import registration  # noqa: F401 — triggers @mcp.tool() decorators at import time
from .modules.lifecycle import main

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hooks.cli import run_hook

        sys.exit(run_hook(sys.argv[2:]))
    main()
