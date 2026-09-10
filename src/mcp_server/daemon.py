"""
Background Daemon Launcher — Singleton socket server for hooks.

Serializes file I/O across multiple IDEs by running all hooks in a single process.
"""

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import settings
from .helpers.logger import logger
from .hooks.handler import handle_hook
from .hooks.hook_event import HookEvent


def _get_port_file() -> Path:
    return settings.config_home / "daemon.port"


# A daemon with no hook traffic for this long exits on its own (self-cleanup).
# Hooks are sporadic, so a leftover/orphaned daemon (whose transient spawner
# exited long ago) must not linger forever — it idles out and is respawned on
# the next hook demand via ``send_to_daemon``.
_DAEMON_IDLE_SECONDS = 300
_DAEMON_CHECK_SECONDS = 5

# Port this daemon bound, set by ``start_daemon_server``; ``run_daemon`` only
# unlinks the shared port file while it still points at us (never clobber a
# replacement daemon's port).
_MY_PORT: int | None = None


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await reader.read()
        if not data:
            return

        payload = json.loads(data.decode("utf-8"))
        hook = HookEvent(**payload["hook_event"])

        result = handle_hook(hook)

        writer.write(json.dumps(result).encode("utf-8"))
        await writer.drain()
    except Exception as e:
        logger.error(f"Daemon handler error: {e}")
        writer.write(json.dumps({"error": str(e)}).encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_daemon_server() -> None:
    global _MY_PORT
    last_activity = {"t": time.monotonic()}

    async def _handle_with_activity(reader, writer) -> None:
        last_activity["t"] = time.monotonic()
        await _handle_client(reader, writer)

    server = await asyncio.start_server(_handle_with_activity, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _MY_PORT = port

    port_file = _get_port_file()
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port), encoding="utf-8")

    logger.info(f"Daemon listening on 127.0.0.1:{port}")

    # Serve until (a) idle — a leftover/orphaned daemon must not linger forever —
    # or (b) the shared port file no longer points at us (a replacement daemon
    # took over, so this one is redundant and must not clobber it on exit).
    task = asyncio.ensure_future(server.serve_forever())
    try:
        while True:
            await asyncio.sleep(_DAEMON_CHECK_SECONDS)
            idle = time.monotonic() - last_activity["t"] > _DAEMON_IDLE_SECONDS
            lost_ownership = not port_file.is_file() or port_file.read_text(encoding="utf-8").strip() != str(port)
            if idle:
                logger.info(f"Daemon idle for {_DAEMON_IDLE_SECONDS}s — exiting")
                break
            if lost_ownership:
                logger.info("Daemon superseded by another listener — exiting")
                break
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def run_daemon() -> None:
    """Entry point for the background daemon process."""
    settings.bootstrap()
    try:
        asyncio.run(start_daemon_server())
    except KeyboardInterrupt:
        pass
    finally:
        # Only unlink while we still own the port file (never remove a
        # replacement daemon's port).
        if _MY_PORT is not None:
            pf = _get_port_file()
            try:
                if pf.is_file() and pf.read_text(encoding="utf-8").strip() == str(_MY_PORT):
                    pf.unlink(missing_ok=True)
            except OSError:
                pass


def send_to_daemon(hook: HookEvent) -> dict[str, Any]:
    """Client function: forward hook to daemon, spawning it if necessary."""
    settings.bootstrap()
    port_file = _get_port_file()

    for attempt in range(2):
        if port_file.exists():
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(("127.0.0.1", port))
                    s.sendall(json.dumps({"hook_event": hook.model_dump()}).encode("utf-8"))
                    s.shutdown(socket.SHUT_WR)

                    resp = []
                    while True:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        resp.append(chunk)
                    return json.loads(b"".join(resp).decode("utf-8"))
            except (ValueError, ConnectionRefusedError, socket.timeout, json.JSONDecodeError):
                port_file.unlink(missing_ok=True)

        if attempt == 0:
            # Spawn daemon using the exact same executable or python invocation
            cmd = [sys.executable]
            if not getattr(sys, "frozen", False):
                # If running via python source
                cmd.extend(["-m", "mcp_server"])
            cmd.append("daemon")

            try:
                if sys.platform == "win32":
                    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logger.warning(f"Failed to spawn daemon: {e}")
                break

            # Wait for port file to appear
            for _ in range(15):
                if port_file.exists():
                    break
                time.sleep(0.1)

    # Fallback to in-process execution if daemon totally fails
    logger.warning("Daemon unavailable, falling back to in-process handle_hook")
    return handle_hook(hook)
