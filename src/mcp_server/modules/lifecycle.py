"""
Server lifecycle — FastMCP app creation, config, and entry point.

Contains:
- FastMCP instance creation
- main() entry point
"""

import os
import signal

from mcp.server.fastmcp import FastMCP

from .._version import VERSION_STRING
from ..config import settings
from ..helpers.logger import Logger
from .graphify.config import request_shutdown

# ── App Instance ─────────────────────────────────────────────────────────────

mcp = FastMCP("AWLab-AI-Assistant")


def _install_worker_signal_handlers(lgr: Logger) -> None:
    """Handle SIGINT/SIGTERM in the worker: cancel in-flight rebuilds and exit.

    Without this, a graceful signal (POSIX kill / Ctrl-C) terminates the worker
    while a background graph rebuild may be mid-extract. Requesting shutdown lets
    the chunk loop stop between chunks; we then leave with 128+signum semantics.
    (On Windows a hard ``taskkill /F`` / ``os.kill`` SIGTERM uses TerminateProcess
    and never runs Python handlers — the bridge teardown covers that path.)
    """
    try:

        def _on_signal(signum: int, _frame) -> None:
            request_shutdown()
            lgr.warning(f"worker received signal {signum} — cancelling in-flight work and exiting")
            os._exit(128 + signum)

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:  # pragma: no cover - defensive
        pass


def _start_orphan_watchdog(lgr: Logger) -> None:
    """Self-terminate if the parent bridge dies (orphan prevention, all OSes).

    The bridge passes its PID via ``AWLAB_BRIDGE_PID`` when it spawns this worker.
    A daemon watchdog polls that parent; if it disappears (bridge killed, host
    closed, bootloader reaped) the worker exits instead of leaking. No-op when the
    env var is unset (dev/standalone ``python -m mcp_server``).
    """
    try:
        from ..bridge import AWLAB_BRIDGE_PID_ENV, watch_parent
    except Exception:  # pragma: no cover - bridge module unavailable in some builds
        return
    bridge_pid = os.environ.get(AWLAB_BRIDGE_PID_ENV, "").strip()
    if not bridge_pid:
        return

    def _orphan_exit() -> None:
        lgr.warning(f"parent bridge pid={bridge_pid} died — worker exiting (orphan watchdog)")
        # Cancel in-flight background rebuilds first so a mid-extract chunk stops
        # cleanly; then leave. os._exit: the FastMCP stdio loop owns the main
        # thread and is blocked; a daemon thread cannot unwind it.
        request_shutdown()
        os._exit(0)

    try:
        watch_parent(int(bridge_pid), _orphan_exit)
        lgr.info(f"orphan watchdog armed — watching parent bridge pid={bridge_pid}")
    except Exception as e:  # pragma: no cover - defensive
        lgr.warning(f"orphan watchdog failed to start: {e}")


# ── Shared runner — used by all server entry points ─────────────────────────


def run_server(mcp_instance: FastMCP, server_name: str = "AWLab-AI-Assistant") -> None:
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

    # Start the async bake scheduler (three-tier orchestration — tier 1).
    try:
        from .bake_scheduler import start_scheduler

        start_scheduler()
    except Exception:
        lgr.warning("Async bake scheduler failed to start")

    # Orphan watchdog: if the bridge that spawned us dies, exit instead of leaking.
    _start_orphan_watchdog(lgr)
    # SIGINT/SIGTERM: cancel in-flight rebuilds and exit promptly (clean shutdown).
    _install_worker_signal_handlers(lgr)

    mcp_instance.run(transport="stdio")

    # Reached when the stdio transport ends (client closed stdin) or the server
    # stops. Cancel any in-flight rebuild, then guarantee the worker process
    # fully exits even if a library thread (non-daemon) would keep it alive.
    request_shutdown()
    lgr.info(f"MCP Server [{server_name}] stopped")
    os._exit(0)


# ── Entry Point (original, backwards-compatible) ────────────────────────────


def main():
    """Run the default MCP server on stdio transport."""
    run_server(mcp, server_name="AWLab-AI-Assistant")


if __name__ == "__main__":
    main()
