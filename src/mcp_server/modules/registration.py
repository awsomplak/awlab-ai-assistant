"""
Tool registration for awlab-mcp server — 2-tool dispatcher surface.

Exposes only ``action_call`` + ``action_help`` on the shared FastMCP instance.
The full action surface is driven by the REGISTRY (src/mcp_server/registry.py).
"""

from .dispatcher import register_dispatcher
from .lifecycle import mcp

register_dispatcher(mcp)
