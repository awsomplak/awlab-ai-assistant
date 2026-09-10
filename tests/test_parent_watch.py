"""Parent-death watchdog tests — orphan self-termination (all OSes).

Validates the bridge -> worker orphan fix:
- ``_process_alive`` detects a live vs dead parent.
- ``watch_parent`` fires ``on_death`` only after the parent exits.
- ``watch_parent`` is disabled when no bridge PID is known (dev/standalone).
- ``spawn_worker`` passes ``AWLAB_BRIDGE_PID`` to the child it spawns.
- A real worker subprocess exits when its stub parent is killed (integration).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

_SLEEP = "import time; time.sleep(60)"


def _spawn_stub() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", _SLEEP])


def _wait_until(predicate, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_process_alive_true_then_false():
    from mcp_server.bridge import _process_alive

    stub = _spawn_stub()
    try:
        assert _process_alive(stub.pid) is True
    finally:
        stub.kill()
        stub.wait()
    assert _wait_until(lambda: not _process_alive(stub.pid))
    assert _process_alive(stub.pid) is False


def test_process_alive_false_for_unknown_pid():
    from mcp_server.bridge import _process_alive

    assert _process_alive(2**31 - 1) is False  # implausibly high PID


def test_watch_parent_fires_only_after_parent_death():
    from mcp_server.bridge import watch_parent

    stub = _spawn_stub()
    fired: list = []
    try:
        watch_parent(stub.pid, lambda: fired.append(True), interval=0.1)
        time.sleep(0.5)
        assert fired == []  # parent still alive -> must not fire
        stub.kill()
        stub.wait()
        assert _wait_until(lambda: bool(fired))
    finally:
        if stub.poll() is None:
            stub.kill()
    assert fired == [True]


def test_watch_parent_disabled_without_bridge_pid():
    from mcp_server.bridge import watch_parent

    assert watch_parent(None, lambda: None) is None
    assert watch_parent("", lambda: None) is None
    assert watch_parent(0, lambda: None) is None
    # A process must never watch itself.
    assert watch_parent(os.getpid(), lambda: None) is None


def test_spawn_worker_sets_bridge_pid_env(monkeypatch, tmp_path):
    from mcp_server import bridge

    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")

    monkeypatch.setattr(bridge, "_worker_log_file", lambda: tmp_path / "w.log")
    monkeypatch.setattr(bridge.subprocess, "Popen", FakePopen)
    bridge.spawn_worker(["fake-worker"])
    assert captured["cmd"] == ["fake-worker"]
    assert captured["env"] is not None
    assert captured["env"].get(bridge.AWLAB_BRIDGE_PID_ENV) == str(os.getpid())


def test_worker_exits_when_stub_parent_dies():
    """End-to-end: a worker process self-terminates once its bridge parent dies."""
    from mcp_server.bridge import AWLAB_BRIDGE_PID_ENV

    parent = _spawn_stub()
    worker_code = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {SRC!r})\n"
        "from mcp_server.bridge import watch_parent\n"
        "def _die():\n"
        "    os._exit(0)\n"
        "watch_parent(int(os.environ[%r]), _die, interval=0.1)\n"
        % AWLAB_BRIDGE_PID_ENV
        + 'print("armed", flush=True)\n'
        "time.sleep(120)\n"
    )
    env = {**os.environ, AWLAB_BRIDGE_PID_ENV: str(parent.pid)}
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        # Wait for the worker to arm its watchdog before killing the parent.
        assert worker.stdout is not None
        assert _wait_until(lambda: "armed" in (worker.stdout.readline() or ""), timeout=15.0)
        parent.kill()
        parent.wait()
        assert _wait_until(lambda: worker.poll() is not None, timeout=8.0)
    finally:
        if worker.poll() is None:
            worker.kill()
        if parent.poll() is None:
            parent.kill()
    assert worker.returncode == 0
