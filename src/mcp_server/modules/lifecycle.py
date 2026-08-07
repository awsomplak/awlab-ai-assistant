"""
Server lifecycle — FastMCP app creation, config, and entry point.

Contains:
- FastMCP instance creation
- _wrap() helper for bridge operations
- main() entry point
"""

import json

from mcp.server.fastmcp import FastMCP

from .._version import VERSION_STRING
from ..config import settings
from ..helpers import logger as logger  # module-level singleton (used by _wrap)
from ..helpers.logger import Logger

# ── App Instance ─────────────────────────────────────────────────────────────

mcp = FastMCP("awlab-mcp")


# ── Bridge Helpers ───────────────────────────────────────────────────────────


def _wrap(fn) -> str:
    """Execute a bridge operation and return JSON result."""
    try:
        result = fn()
        return json.dumps({"success": True, "result": result})
    except Exception as e:
        logger.error(f"_wrap bridge operation failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ── Shared runner — used by all server entry points ─────────────────────────


def run_server(mcp_instance: FastMCP, server_name: str = "agent-memory") -> None:
    """Run an MCP server instance on stdio transport with full initialization."""
    # Bootstrap settings (load .env + config.json)
    settings.bootstrap()

    # Re-create logger with resolved log directory
    lgr = Logger(log_dir=settings.log_dir)

    mode = "PRODUCTION" if settings.is_production else "DEVELOPMENT"
    lgr.info(f"MCP Server [{server_name}] starting — {VERSION_STRING}  [{mode}]")
    lgr.debug(f"Log level: {settings.log_level}, Enabled: {settings.enable_log}")
    lgr.debug(f"Log directory: {settings.log_dir}")
    lgr.debug(f"Config home: {settings.config_home}")
    lgr.debug(f"Production mode: {settings.is_production}")

    # Pre-download embedding model at startup in background (fastembed only)
    try:
        import threading

        def _dl():
            try:
                from ..helpers.embeddings import ensure_model_downloaded

                ensure_model_downloaded()
            except Exception:
                pass

        t = threading.Thread(target=_dl, daemon=True)
        t.start()
    except Exception:
        lgr.warning("Embedding model pre-download thread failed")

    mcp_instance.run(transport="stdio")

    lgr.info(f"MCP Server [{server_name}] stopped")


# ── Entry Point (original, backwards-compatible) ────────────────────────────


def main():
    """Run the default MCP server on stdio transport."""
    run_server(mcp, server_name="awlab-mcp")


if __name__ == "__main__":
    main()
