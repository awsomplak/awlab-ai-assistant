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
    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    port_file = _get_port_file()
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port), encoding="utf-8")

    logger.info(f"Daemon listening on 127.0.0.1:{port}")

    async with server:
        await server.serve_forever()


def run_daemon() -> None:
    """Entry point for the background daemon process."""
    settings.bootstrap()
    try:
        asyncio.run(start_daemon_server())
    except KeyboardInterrupt:
        pass
    finally:
        _get_port_file().unlink(missing_ok=True)


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
