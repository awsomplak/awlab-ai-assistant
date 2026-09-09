"""Bridge process-tree teardown tests (Phase 3).

Validates that terminating a worker via ``_terminate_worker_tree`` reaps the
worker AND any processes it spawned (the orphan chain), on the local OS.
POSIX: the worker is spawned as a session leader (like ``spawn_worker``) so
``os.killpg(worker.pid)`` targets only its own process group. Windows:
``taskkill /T`` reaps the child tree.
"""

import os
import subprocess
import sys
import time

WORKER_CODE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _spawn_worker_with_child():
    """Spawn a fake worker that itself spawns a grandchild; mirror spawn_worker."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True  # worker becomes its own process group
    worker = subprocess.Popen(
        [sys.executable, "-c", WORKER_CODE],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **kwargs,
    )
    assert worker.stdout is not None
    child_pid = int(worker.stdout.readline().strip())
    return worker, child_pid


def _wait_gone(pid, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.1)
    return False


def _pid_exists(pid):
    from mcp_server.bridge import _process_alive

    return _process_alive(pid)


def test_terminate_worker_tree_reaps_worker_and_child():
    from mcp_server.bridge import _terminate_worker_tree

    worker, child_pid = _spawn_worker_with_child()
    try:
        assert _pid_exists(worker.pid)
        assert _pid_exists(child_pid)
        _terminate_worker_tree(worker, grace=1.0)
        assert _wait_gone(worker.pid), f"worker {worker.pid} survived tree kill"
        assert _wait_gone(child_pid), f"worker child {child_pid} survived tree kill"
    finally:
        if _pid_exists(child_pid):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(child_pid)], capture_output=True)
        if worker.poll() is None:
            worker.kill()


def test_terminate_worker_tree_noop_for_dead_or_none():
    from mcp_server.bridge import _terminate_worker_tree

    _terminate_worker_tree(None)  # must not raise
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()  # already exited
    _terminate_worker_tree(p)  # must not raise for a dead process


def test_assign_worker_tree_kill_no_error():
    """Job-object assignment (Windows) / no-op (POSIX) must not raise."""
    from mcp_server.bridge import _assign_worker_tree_kill

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _assign_worker_tree_kill(p)  # no exception expected either way
    finally:
        p.kill()
        p.wait()
