"""Bridge tests — stdio passthrough, hot-reload, lock lifecycle, publish safety.

Formalizes the Phase 1-4 behavior that was first proven in throwaway smoke
harnesses:

* 6.1 worker spawn/terminate, stdio passthrough, hot-reload respawn on lock clear
* 6.2 lock lifecycle (absent / stale-TTL / clears) + N-bridge concurrent wait
* 6.3 publish sequence stops the worker but leaves the bridge alive (no EOF)
* 6.4 a hook arriving while `.update_lock` is held waits, then runs against the
      freshly swapped binary (never execs a half-written worker)

The bridge is spawned as ``python -m mcp_server.bridge`` with ``AWLAB_WORKER``
pointing at a tiny stdlib-only fake worker (so no FastMCP/heavy deps load), and
``AWLAB_UPDATE_LOCK`` pointing into the pytest ``tmp_path`` so the real
``~/.awlab-id`` deployment is never touched.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mcp_server import bridge as bridge_mod

# ══════════════════════════════════════════════════════════════════════════
#  Fake workers (stdlib-only; written to tmp_path by fixtures)
# ══════════════════════════════════════════════════════════════════════════

FAKE_ECHO = r"""#!/usr/bin/env python3
import os, sys
pf = os.environ.get("AWLAB_PIDFILE")
if pf:
    with open(pf, "w") as f:
        f.write(str(os.getpid()))
for line in sys.stdin.buffer:
    sys.stdout.buffer.write(b"echo:" + line)
    sys.stdout.buffer.flush()
sys.stdout.buffer.write(b"__EOF__\n")
sys.stdout.buffer.flush()
"""

FAKE_HOOK = r"""#!/usr/bin/env python3
import os, sys, time
if len(sys.argv) > 1 and sys.argv[1] == "hook":
    data = sys.stdin.read()
    agent = sys.argv[3] if len(sys.argv) > 3 else "?"
    sys.stdout.write('{"result": "hook-ran", "agent": "%s", "stdin_len": %d}\n' % (agent, len(data)))
    sys.stdout.flush()
    sys.exit(0)
if len(sys.argv) > 1 and sys.argv[1] == "daemon":
    pf = os.environ.get("AWLAB_DAEMON_PIDFILE")
    if pf:
        with open(pf, "w") as f:
            f.write(str(os.getpid()))
    time.sleep(60)
    sys.exit(0)
for line in sys.stdin.buffer:
    sys.stdout.buffer.write(b"echo:" + line)
    sys.stdout.buffer.flush()
sys.stdout.buffer.write(b"__EOF__\n")
sys.stdout.buffer.flush()
"""


@pytest.fixture
def echo_worker(tmp_path):
    p = tmp_path / "fake_worker.py"
    p.write_text(FAKE_ECHO)
    p.chmod(0o755)
    return p


@pytest.fixture
def hook_worker(tmp_path):
    p = tmp_path / "fake_worker2.py"
    p.write_text(FAKE_HOOK)
    p.chmod(0o755)
    return p


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _base_env(tmp_path, worker, lock) -> dict:
    env = dict(os.environ)
    env["AWLAB_ENV"] = "development"
    env["AWLAB_WORKER"] = str(worker)
    env["AWLAB_UPDATE_LOCK"] = str(lock)
    return env


def spawn_bridge(tmp_path, worker, lock, pidfile=None, args=()):
    """Spawn a dev bridge; capture its stdout with a raw-fd pump.

    ``p.stdout.read`` would block until 64 KiB or EOF on a pipe (BufferedReader),
    so we read the raw fd with ``os.read`` which returns as soon as bytes arrive.
    """
    env = _base_env(tmp_path, worker, lock)
    if pidfile is not None:
        env["AWLAB_PIDFILE"] = str(pidfile)
    err_path = tmp_path / f"bridge-{time.monotonic_ns()}.stderr.log"
    err_fh = open(err_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.bridge", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=err_fh,
        env=env,
        cwd=str(tmp_path),
    )
    state = {"data": b"", "eof": False, "lock": threading.Lock()}

    def pump():
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    with state["lock"]:
                        state["eof"] = True
                    return
                with state["lock"]:
                    state["data"] += chunk
        except (OSError, ValueError):
            with state["lock"]:
                state["eof"] = True

    threading.Thread(target=pump, daemon=True).start()
    return proc, state, err_path


def out_text(state) -> str:
    with state["lock"]:
        return state["data"].decode("utf-8", "replace")


def wait_until(cond, timeout=15.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def worker_pid(pidfile: Path) -> int:
    try:
        return int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return 0


def kill_graceful(proc) -> None:
    if proc and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  6.1 — worker spawn / stdio passthrough / clean exit
# ══════════════════════════════════════════════════════════════════════════


def test_stdio_passthrough_and_clean_exit(tmp_path, echo_worker):
    lock = tmp_path / "bin" / ".update_lock"
    pidfile = tmp_path / "w.pid"
    proc, state, _ = spawn_bridge(tmp_path, echo_worker, lock, pidfile)
    try:
        assert wait_until(lambda: worker_pid(pidfile) > 0), "worker never spawned"
        proc.stdin.write(b"ping\n")
        proc.stdin.flush()
        assert wait_until(lambda: "echo:ping" in out_text(state)), "echo not forwarded"
        assert proc.poll() is None, "bridge died during normal operation"
        proc.stdin.close()
        rc = proc.wait(timeout=10)
        assert rc == 0, f"bridge did not exit cleanly on stdin EOF (rc={rc})"
        assert "echo:ping" in out_text(state)
    finally:
        kill_graceful(proc)


def test_bridge_sigterm_terminates_worker_and_exits(tmp_path, echo_worker):
    lock = tmp_path / "bin" / ".update_lock"
    pidfile = tmp_path / "w.pid"
    proc, state, _ = spawn_bridge(tmp_path, echo_worker, lock, pidfile)
    try:
        assert wait_until(lambda: worker_pid(pidfile) > 0), "worker never spawned"
        wid = worker_pid(pidfile)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) is not None, "bridge did not exit after SIGTERM"
        # The worker must have been reaped, not orphaned.
        assert wait_until(lambda: _pid_alive(wid) is False), "worker survived bridge SIGTERM"
    finally:
        kill_graceful(proc)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════
#  6.1 — hot-reload respawn on lock clear
# ══════════════════════════════════════════════════════════════════════════


def test_hot_reload_respawn_replays_inflight_bytes(tmp_path, echo_worker):
    lock = tmp_path / "bin" / ".update_lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    pidfile = tmp_path / "w.pid"
    proc, state, _ = spawn_bridge(tmp_path, echo_worker, lock, pidfile)
    try:
        assert wait_until(lambda: worker_pid(pidfile) > 0), "worker never spawned"
        pid1 = worker_pid(pidfile)

        # Publisher: hold the lock, then kill the worker.
        lock.write_text("updating")
        os.kill(pid1, signal.SIGTERM)
        time.sleep(0.4)
        assert proc.poll() is None, "bridge died when its worker was killed"

        # Write while the worker is down (lock held) -> must be held, not lost.
        proc.stdin.write(b"two\n")
        proc.stdin.flush()
        time.sleep(0.3)

        # Publisher finishes -> lock clears -> bridge respawns + replays.
        lock.unlink()
        ok = wait_until(lambda: worker_pid(pidfile) not in (0, pid1) and "echo:two" in out_text(state), timeout=15)
        assert ok, f"no respawn/replay (pidfile={worker_pid(pidfile)}, out={out_text(state)!r})"
        assert proc.poll() is None, "bridge died across hot reload"

        # Full duplex still intact -> close stdin, expect clean exit.
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        kill_graceful(proc)


# ══════════════════════════════════════════════════════════════════════════
#  6.2 — lock lifecycle (unit, in-process) + N-bridge coordination
# ══════════════════════════════════════════════════════════════════════════


def test_lock_wait_absent_returns_immediately(tmp_path):
    lock = tmp_path / ".update_lock"
    assert bridge_mod.wait_for_lock_clear(lock, ttl=5.0, poll=0.01) is True


def test_lock_wait_stale_ttl_returns_false(tmp_path):
    lock = tmp_path / ".update_lock"
    lock.write_text("stale")
    t0 = time.monotonic()
    result = bridge_mod.wait_for_lock_clear(lock, ttl=0.3, poll=0.02)
    assert result is False, "stale lock must time out, not block forever"
    assert time.monotonic() - t0 < 3.0


def test_lock_wait_blocks_until_cleared(tmp_path):
    lock = tmp_path / ".update_lock"
    lock.write_text("updating")

    def clear_later():
        time.sleep(0.3)
        lock.unlink()

    threading.Thread(target=clear_later, daemon=True).start()
    assert bridge_mod.wait_for_lock_clear(lock, ttl=5.0, poll=0.02) is True


def test_default_lock_path_honors_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / ".update_lock"
    monkeypatch.setenv("AWLAB_UPDATE_LOCK", str(custom))
    assert bridge_mod.default_lock_path() == custom


def test_n_bridges_coordinate_on_single_lock(tmp_path, echo_worker):
    lock = tmp_path / "bin" / ".update_lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    n = 3
    pidfiles = [tmp_path / f"w{i}.pid" for i in range(n)]
    procs = [spawn_bridge(tmp_path, echo_worker, lock, pidfiles[i])[0] for i in range(n)]
    try:
        assert wait_until(lambda: all(worker_pid(p) > 0 for p in pidfiles)), "workers never spawned"
        old = [worker_pid(p) for p in pidfiles]

        lock.write_text("updating")
        for pid in old:
            os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        assert all(p.poll() is None for p in procs), "a bridge died while the lock was held"

        lock.unlink()
        ok = wait_until(lambda: all(worker_pid(pidfiles[i]) not in (0, old[i]) for i in range(n)), timeout=15)
        assert ok, "not every bridge respawned a fresh worker"
        assert all(p.poll() is None for p in procs), "a bridge died after respawn"
        assert not lock.exists(), "leftover .update_lock"
    finally:
        for p in procs:
            kill_graceful(p)


# ══════════════════════════════════════════════════════════════════════════
#  6.3 — publish stops the worker but leaves the bridge alive (no EOF)
# ══════════════════════════════════════════════════════════════════════════


def test_publish_never_eofs_bridge_stdio(tmp_path, echo_worker):
    lock = tmp_path / "bin" / ".update_lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    pidfile = tmp_path / "w.pid"
    proc, state, _ = spawn_bridge(tmp_path, echo_worker, lock, pidfile)
    try:
        assert wait_until(lambda: worker_pid(pidfile) > 0), "worker never spawned"
        pid1 = worker_pid(pidfile)

        # Sanity: the pipe works before the update.
        proc.stdin.write(b"one\n")
        proc.stdin.flush()
        assert wait_until(lambda: "echo:one" in out_text(state)), "baseline echo missing"

        # Publisher: lock -> kill worker.
        lock.write_text("updating")
        os.kill(pid1, signal.SIGTERM)
        time.sleep(0.5)

        # The critical guarantee: the bridge process is alive AND its stdout pipe
        # never saw EOF (the IDE would surface "context canceled" on EOF).
        assert proc.poll() is None, "bridge died during publish"
        with state["lock"]:
            assert state["eof"] is False, "bridge stdout hit EOF during publish"

        # Even with the worker down, the IDE can still write (no EPIPE/backpressure).
        proc.stdin.write(b"two\n")
        proc.stdin.flush()

        # Publisher finishes -> lock clears -> new worker, both bytes echoed back.
        lock.unlink()
        assert wait_until(lambda: "echo:two" in out_text(state), timeout=15), "no echo after respawn"
        assert "echo:one" in out_text(state)
        assert proc.poll() is None and (not state["eof"])
    finally:
        kill_graceful(proc)


# ══════════════════════════════════════════════════════════════════════════
#  6.4 — hook during an update waits on the lock (never execs partial binary)
# ══════════════════════════════════════════════════════════════════════════


def _run_hook(tmp_path, worker, lock, timeout=15.0):
    env = _base_env(tmp_path, worker, lock)
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_server.bridge", "hook", "--agent", "claude", "--event", "Test"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )


def test_hook_without_lock_executes_immediately(tmp_path, hook_worker):
    lock = tmp_path / ".update_lock"
    proc = _run_hook(tmp_path, hook_worker, lock)
    proc.stdin.write(b"{}\n")
    proc.stdin.flush()
    proc.stdin.close()
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, f"hook failed: {err.decode(errors='replace')}"
    assert b"hook-ran" in out and b"claude" in out


def test_hook_during_update_waits_then_executes(tmp_path, hook_worker):
    lock = tmp_path / ".update_lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("updating")  # publish in flight

    proc = _run_hook(tmp_path, hook_worker, lock)
    proc.stdin.write(b"{}\n")
    proc.stdin.flush()
    proc.stdin.close()

    # The hook must be blocked (alive), waiting for the lock, NOT exec'ing a
    # half-written worker.
    assert wait_until(lambda: proc.poll() is not None, timeout=1.0) is False, "hook exited early"
    assert proc.poll() is None, "hook should wait while .update_lock is present"

    # Publisher finishes -> lock clears -> hook completes against the new binary.
    lock.unlink()
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, f"hook failed after lock cleared: {err.decode(errors='replace')}"
    assert b"hook-ran" in out and b"claude" in out


# ══════════════════════════════════════════════════════════════════════════
#  Hot-reload MCP session replay (regression)
#  A respawned worker has no MCP session and rejects every request (-32602)
#  until it receives `initialize`. A real client never re-initializes because the
#  bridge (its server process) does not restart — so the bridge must replay the
#  captured handshake to each fresh worker.
# ══════════════════════════════════════════════════════════════════════════

FAKE_MCP = r"""#!/usr/bin/env python3
import json, os, sys
pf = os.environ.get("AWLAB_PIDFILE")
if pf:
    with open(pf, "w") as f:
        f.write(str(os.getpid()))
initialized = False
for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake-mcp", "version": "1.0"}}}) + "\n")
        sys.stdout.flush()
    elif method == "notifications/initialized":
        initialized = True
    elif method == "tools/list":
        if not initialized:
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": "Invalid request parameters"}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid,
                    "result": {"tools": [{"name": "fake_tool"}]}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
"""


@pytest.fixture
def mcp_worker(tmp_path):
    p = tmp_path / "fake_mcp_worker.py"
    p.write_text(FAKE_MCP)
    p.chmod(0o755)
    return p


def send_mcp(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def find_response(state, want_id):
    for raw in out_text(state).splitlines():
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("id") == want_id:
            return obj
    return None


def test_hot_reload_replays_initialize_handshake(tmp_path, mcp_worker):
    lock = tmp_path / "bin" / ".update_lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    pidfile = tmp_path / "m.pid"
    proc, state, _ = spawn_bridge(tmp_path, mcp_worker, lock, pidfile)
    try:
        assert wait_until(lambda: worker_pid(pidfile) > 0), "worker never spawned"
        pid1 = worker_pid(pidfile)

        # Client handshake + baseline call.
        send_mcp(proc, {"jsonrpc": "2.0", "id": 100, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "1"}}})
        assert wait_until(lambda: find_response(state, 100) is not None), "no initialize response"
        assert find_response(state, 100).get("result", {}).get("serverInfo")
        send_mcp(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send_mcp(proc, {"jsonrpc": "2.0", "id": 101, "method": "tools/list", "params": {}})
        assert wait_until(lambda: find_response(state, 101) is not None), "no baseline tools/list"
        assert "error" not in find_response(state, 101), f"baseline failed: {find_response(state, 101)}"

        # Publish-style worker swap (lock -> kill worker -> unlock -> respawn).
        lock.write_text("updating")
        os.kill(pid1, signal.SIGTERM)
        time.sleep(0.4)
        lock.unlink()
        assert wait_until(lambda: worker_pid(pidfile) not in (0, pid1)), "worker not respawned"

        # Client keeps using the SAME session (no re-initialize). The bridge must
        # have replayed initialize + initialized to the fresh worker.
        send_mcp(proc, {"jsonrpc": "2.0", "id": 102, "method": "tools/list", "params": {}})
        assert wait_until(lambda: find_response(state, 102) is not None), "no post-reload tools/list"
        resp = find_response(state, 102)
        assert "error" not in resp, f"fresh worker rejected request without replay: {resp}"
        assert resp.get("result", {}).get("tools") == [{"name": "fake_tool"}]
    finally:
        kill_graceful(proc)

