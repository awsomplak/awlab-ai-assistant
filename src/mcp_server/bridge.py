"""
MCP Bridge — persistent stdio proxy with worker hot-reload.

Why this exists
---------------
IDE/agent hosts launch an MCP server over stdio and hold the JSON-RPC pipe open
for the whole session. If the server process is killed — for example to overwrite
its binary during a publish — the pipe dies and every connected IDE surfaces
``context canceled``. This bridge is the persistent middleman that owns the pipe
so the heavy server can be swapped underneath it::

    IDE  <->  awlab-ai-assistant (bridge, this process)  <->  awlab-ai-worker (MCP)

The bridge locates the worker, spawns it, forwards stdin/stdout with raw
descriptor copies (near-zero overhead), and on worker exit polls the shared
``.update_lock`` file the publisher leaves behind. It waits for the lock to
clear, then respawns the freshly published worker and resumes traffic without
the IDE ever seeing EOF.

Roles / naming
--------------
* ``awlab-ai-assistant`` — the bridge (this module). Stable entrypoint for every
  host; its name never changes.
* ``awlab-ai-worker``    — the actual MCP stdio server (the ``mcp_server``
  package frozen with ``__main__`` as its entry). It is replaced during publish.

Concurrency
-----------
Each connected IDE spawns its own bridge -> its own worker. All bridges on the
machine coordinate through one user-global ``.update_lock`` in the shared bin
dir, so a single publish restarts every worker without restarting any bridge.

Design notes
------------
* stdin/stdout forwarding is unbuffered raw-fd I/O (no per-request buffering).
* A single persistent reader owns the IDE stdin, so worker restarts never risk
  two readers splitting the stream, and bytes that arrive while the worker is
  down are held in order and replayed to the replacement worker.
* Worker stderr is routed to a per-process log file, never to the IDE pipe.
* The update-lock wait is bounded by a stale-lock TTL so a crashed publish can
  never hang a bridge forever.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

# NOTE: This module intentionally imports ONLY the standard library. It is the
# PyInstaller entry for the bridge executable (awlab-ai-assistant), and importing
# mcp_server would pull in the heavy helpers stack (fastembed / lancedb / ...),
# bloating the tiny bridge bundle. The logging + config-home resolution below are
# self-contained mirrors of src/mcp_server/config.py and helpers/logger.py.

# ══════════════════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════════════════

# Worker binary basename. The bridge ships as `awlab-ai-assistant` (its name is
# the stable entrypoint for every host) and spawns `awlab-ai-worker` as the heavy
# MCP server so it can be swapped underneath a live bridge.
WORKER_NAME = "awlab-ai-worker"

# File the publisher writes before stopping workers and removes after copying.
LOCK_NAME = ".update_lock"

# Raw-fd copy chunk (64 KiB) — the same order as the OS pipe buffer.
_CHUNK = 65536

# Update-lock polling. The wait is bounded by the stale-lock TTL so a crashed
# publish can never wedge a bridge forever.
LOCK_POLL_SECONDS = 0.05
LOCK_TTL_SECONDS = 30.0

# Small settle between a crash-respawn and re-checking the lock so a worker that
# dies instantly (e.g. bad config) cannot spin the main loop into a tight loop.
CRASH_BACKOFF_SECONDS = 0.25

# Env var the bridge sets on the worker it spawns so the worker can detect when
# its parent (the real bridge) dies and self-terminate instead of leaking as an
# orphan (all OSes). Left unset in dev/standalone runs, where no watchdog runs.
AWLAB_BRIDGE_PID_ENV = "AWLAB_BRIDGE_PID"

# Parent-liveness poll interval for the orphan watchdog.
PARENT_WATCH_INTERVAL_SECONDS = 1.0

# How long the bridge waits for the worker to exit on its own after stdin EOF /
# shutdown before force-killing the worker tree (a busy worker can ignore EOF).
EOF_GRACE_SECONDS = 5.0


# ══════════════════════════════════════════════════════════════════════════
#  Self-contained logging & paths (stdlib only — keeps the bridge bundle tiny)
# ══════════════════════════════════════════════════════════════════════════


def _is_production() -> bool:
    """Production when frozen or AWLAB_ENV=production (mirrors config.py)."""
    if _is_frozen():
        return True
    return os.environ.get("AWLAB_ENV", "").strip().lower() in ("production", "prod")


def _config_home() -> Path:
    """User-global config dir — keep in sync with src/mcp_server/config.py."""
    return Path.home() / ".awlab-id" / "agent-memory"


def _log_dir() -> Path:
    """Log directory: production -> config-home/logs, development -> ./logs."""
    if _is_production():
        path = _config_home() / "logs"
    else:
        path = Path.cwd() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class _BridgeLog:
    """Minimal INFO/WARNING/ERROR logger to a daily file (+ errors to stderr).

    Stdlib-only on purpose: importing mcp_server.helpers here would eagerly pull
    fastembed/lancedb/... into the frozen bridge (a ~160 MB onefile). The format
    mirrors helpers/logger.py.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def _write(self, level: str, message: str) -> None:
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        line = f"[{ts}] {level:5s}  [bridge] {message}"
        if level == "ERROR":
            print(line, file=sys.stderr, flush=True)
        try:
            with self._lock:
                log_path = _log_dir() / f"{now.strftime('%Y-%m-%d')}.log"
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass


log = _BridgeLog()


class WorkerUnavailableError(RuntimeError):
    """Raised when no usable worker executable can be located."""


# ══════════════════════════════════════════════════════════════════════════
#  Shared runtime state
# ══════════════════════════════════════════════════════════════════════════


class _Runtime:
    """Mutable state shared between the reader thread and the main loop.

    ``proc`` is guarded by ``cond`` so the reader can atomically observe worker
    (re)spawns and the main loop can atomically clear it on worker exit.
    """

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None
        self.stdin_eof = False  # IDE closed its side of the pipe
        self.shutdown = False  # signal handler asked us to stop
        # MCP session handshake captured from the client and replayed to every
        # respawned worker: a real MCP client treats the bridge as its server
        # process and never re-sends ``initialize`` across a hot reload, so a
        # fresh worker would otherwise reject all requests (-32602).
        self.init_request: bytes | None = None
        self.init_request_id: object = None
        self.init_notification: bytes | None = None


def _is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


# ══════════════════════════════════════════════════════════════════════════
#  Worker discovery (1.1)
# ══════════════════════════════════════════════════════════════════════════


def resolve_worker_cmd() -> list[str]:
    """Return the command that launches the heavy MCP worker.

    Resolution order:
      1. ``AWLAB_WORKER`` env var — explicit override (used by tests/dev).
      2. Frozen bundle — ``awlab-ai-worker`` (``.exe`` on Windows) sitting next to
         this bridge's executable.
      3. Development — ``python -m mcp_server`` under the current interpreter.

    Raises :class:`WorkerUnavailableError` with a clear message when nothing can
    be resolved (fail loudly rather than silently spawning a missing worker).
    """
    env_worker = os.environ.get("AWLAB_WORKER", "").strip()
    if env_worker:
        # A bare Python script (.py/.pyw) cannot be launched directly on Windows
        # (Popen/os.execv would fail with an "Exec format error"), and is brittle
        # elsewhere too. Run it under the current interpreter so dev/tests can
        # point AWLAB_WORKER at a tiny stdlib-only fake worker cross-platform.
        if env_worker.lower().endswith((".py", ".pyw")):
            return [sys.executable, env_worker]
        return [env_worker]

    if _is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        ext = ".exe" if os.name == "nt" else ""
        worker = exe_dir / f"{WORKER_NAME}{ext}"
        if worker.is_file():
            return [str(worker)]
        raise WorkerUnavailableError(
            f"worker '{worker}' not found next to the bridge. Re-run `run.py publish --target binary` to install it."
        )

    return [sys.executable, "-m", "mcp_server"]


# ══════════════════════════════════════════════════════════════════════════
#  Update-lock helpers (1.5 / 1.6)
# ══════════════════════════════════════════════════════════════════════════


def default_lock_path() -> Path:
    """Resolve the user-global ``.update_lock`` path.

    Production (frozen): the lock sits next to the binaries (the shared bin dir
    where the publisher drops it). Development/tests may point ``AWLAB_UPDATE_LOCK``
    at any file; the default is the shared bin dir under the config home.
    """
    env_lock = os.environ.get("AWLAB_UPDATE_LOCK", "").strip()
    if env_lock:
        return Path(env_lock)
    if _is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = _config_home() / "bin"
    return base / LOCK_NAME


def wait_for_lock_clear(
    lock: Path,
    ttl: float = LOCK_TTL_SECONDS,
    poll: float = LOCK_POLL_SECONDS,
) -> bool:
    """Block (bounded) until ``.update_lock`` disappears.

    Returns ``True`` when the lock is absent/cleared (safe to spawn), or ``False``
    when the lock outlived ``ttl`` — a stale lock from a crashed publish. In the
    stale case the caller should proceed against whatever binary is on disk rather
    than waiting forever.
    """
    if not lock.exists():
        return True
    log.info(f"update lock present ({lock}) — waiting for publish to finish")
    deadline = time.monotonic() + ttl
    while lock.exists():
        if time.monotonic() >= deadline:
            log.warning(f"update lock stale after {ttl:.0f}s — proceeding with existing binaries")
            return False
        time.sleep(poll)
    log.info("update lock cleared — resuming")
    return True


# ══════════════════════════════════════════════════════════════════════════
#  Worker lifecycle (1.2 / 1.7)
# ══════════════════════════════════════════════════════════════════════════


def _worker_log_file() -> Path:
    """Per-process log file that receives the worker's stderr (1.4)."""
    path = _log_dir() / f"bridge-worker-{os.getpid()}.log"
    return path


def _process_alive(pid: int) -> bool:
    """Return True while process ``pid`` exists (stdlib/ctypes only).

    Windows never reparents orphans, so ``os.getppid()`` keeps returning the dead
    parent's PID forever — probe the actual process handle instead (OpenProcess +
    WaitForSingleObject). POSIX uses the conventional ``os.kill(pid, 0)`` existence
    probe (ESRCH → gone; EPERM → exists but not ours).
    """
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False  # already gone (or access denied → treat as gone)
        try:
            # WaitForSingleObject(h, 0) == WAIT_OBJECT_0 (0) means the process has
            # exited; WAIT_TIMEOUT means it is still running.
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    import errno

    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH  # EPERM → alive but owned by another user
    except (ValueError, TypeError):
        return False
    return True


def watch_parent(
    parent_pid: int | str | None,
    on_death,
    interval: float = PARENT_WATCH_INTERVAL_SECONDS,
) -> threading.Thread | None:
    """Start a daemon watchdog that calls ``on_death()`` when ``parent_pid`` exits.

    Returns ``None`` when ``parent_pid`` is empty or is this process (dev/standalone
    or exec-passthrough, where no bridge spawned us) so no watchdog runs. The daemon
    thread polls ``_process_alive``; once the parent is gone it invokes ``on_death``
    exactly once and stops.
    """
    if not parent_pid or int(parent_pid) == os.getpid():
        return None

    def _run() -> None:
        while True:
            time.sleep(interval)
            if not _process_alive(int(parent_pid)):
                name = getattr(on_death, "__name__", "shutdown")
                log.warning(f"parent pid={parent_pid} died — invoking {name}")
                try:
                    on_death()
                finally:
                    return

    t = threading.Thread(target=_run, name="bridge-parent-watch", daemon=True)
    t.start()
    return t


def spawn_worker(cmd: list[str]) -> subprocess.Popen:
    """Spawn the worker with piped stdio.

    POSIX: ``start_new_session`` detaches the worker from the bridge's terminal
    group so terminal signal storms never hit it directly (the bridge forwards
    termination explicitly). Windows: ``CREATE_NO_WINDOW`` keeps a console from
    flashing for every IDE launch. ``bufsize=0`` yields raw (unbuffered) pipes so
    the forwarding threads write with no extra copy. The child inherits our env
    plus ``AWLAB_BRIDGE_PID`` so its orphan watchdog knows which parent to watch.
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True

    stderr_owned = False
    stderr_fh: Any
    try:
        stderr_fh = open(_worker_log_file(), "ab")
        stderr_owned = True
    except OSError:
        stderr_fh = subprocess.DEVNULL

    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_fh,
            bufsize=0,
            env={**os.environ, AWLAB_BRIDGE_PID_ENV: str(os.getpid())},
            **kwargs,
        )
    except BaseException:
        # Popen raised (e.g. FileNotFoundError) — don't leak the log handle.
        if stderr_owned:
            try:
                stderr_fh.close()
            except OSError:
                pass
        raise


# Job handles for Windows kill-on-close worker reaping. Kept referenced for the
# whole bridge lifetime so the OS holds them; when this process dies (even by a
# force-kill that runs no cleanup), the OS closes the last handle and terminates
# every process assigned to the job (the worker AND anything it spawned).
_WIN_JOBS: list = []


def _assign_worker_tree_kill(proc: subprocess.Popen) -> None:
    """Windows: assign the worker to a Job Object with ``KILL_ON_JOB_CLOSE``.

    This is the guarantee that reaps the worker tree even when the bridge is
    force-killed (TerminateProcess / host kill) — no cleanup code runs, but the
    OS kills the job's processes the moment the bridge's job handle closes.
    POSIX: no-op here — teardown uses ``killpg`` on the worker's process group.
    Degrades gracefully (logs + falls back to ``taskkill /T``) if job creation
    or assignment fails (e.g. the worker is already nested in a non-breakaway
    job).
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        # JobObjectExtendedLimitInformation = 9. Only LimitFlags matters here.
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok_set = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        # AssignProcessToJobObject needs PROCESS_SET_QUOTA | PROCESS_TERMINATE on
        # the worker handle; proc._handle (private, from CreateProcess) carries
        # full access. getattr keeps Pylance clean about the private attribute.
        handle = getattr(proc, "_handle", 0)
        ok_assign = bool(ok_set) and bool(handle) and bool(kernel32.AssignProcessToJobObject(job, int(handle)))
        if ok_assign:
            _WIN_JOBS.append(int(job))  # keep the handle alive until process exit
            log.info(f"worker pid={proc.pid} assigned to kill-on-close job object")
    except Exception as e:  # pragma: no cover - defensive; taskkill fallback below
        log.warning(f"job-object assignment failed ({e}) — fall back to taskkill /T")


def _terminate_worker_tree(proc: subprocess.Popen | None, grace: float = 3.0) -> None:
    """Kill the worker AND its whole process tree (every bridge exit path).

    POSIX: the worker is a session leader (``start_new_session``), so its PID is
    its process-group id — ``os.killpg`` SIGTERM then SIGKILL reaches the worker
    and any children it spawned. Windows: ``proc.terminate()`` (TerminateProcess)
    kills ONLY the worker and would orphan its children, so always ``taskkill /T``
    which reaps the whole descendant tree; the kill-on-close Job Object
    (``_assign_worker_tree_kill``) additionally guarantees tree death when this
    bridge process itself dies.
    """
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _make_signal_handler(runtime: _Runtime):
    """Build a SIGINT/SIGTERM handler that tears the worker down and exits."""

    def _handler(signum: int, _frame) -> NoReturn:
        with runtime.cond:
            runtime.shutdown = True
            proc = runtime.proc
            runtime.cond.notify_all()
        if proc is not None:
            _terminate_worker_tree(proc)
        log.warning(f"bridge received signal {signum} — exiting")
        raise SystemExit(128 + signum)

    return _handler


# ══════════════════════════════════════════════════════════════════════════
#  Forwarding (1.3)
# ══════════════════════════════════════════════════════════════════════════


def _write_all(fd: int, data: bytes) -> None:
    """Write ``data`` to ``fd`` handling partial writes."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            continue
        if written <= 0:
            return
        view = view[written:]


def _record_handshake(runtime: _Runtime, payload: bytes) -> None:
    """Capture the client's MCP ``initialize`` / ``initialized`` for replay."""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(obj, dict):
        return
    with runtime.cond:
        if obj.get("jsonrpc") == "2.0" and obj.get("method") == "initialize" and "id" in obj:
            runtime.init_request = payload + b"\n"
            runtime.init_request_id = obj.get("id")
        elif obj.get("jsonrpc") == "2.0" and obj.get("method") == "notifications/initialized":
            runtime.init_notification = payload + b"\n"


def _write_line_to_worker(runtime: _Runtime, payload: bytes) -> bool:
    """Forward one MCP line to the current worker (waiting if none is up).

    Blocks (bounded by cond waits) while a worker is being respawned, so lines
    are delivered in order to the replacement — nothing is dropped or split.
    Returns False when the IDE closed its pipe while no worker was available.
    """
    while True:
        with runtime.cond:
            while runtime.proc is None and not runtime.stdin_eof and not runtime.shutdown:
                runtime.cond.wait(timeout=0.1)
            if runtime.proc is None:
                return False  # EOF/shutdown while no worker is up
            proc = runtime.proc
        stdin = proc.stdin
        if stdin is None:  # pragma: no cover - spawn always pipes stdin
            return False
        try:
            _write_all(stdin.fileno(), payload + b"\n")
            return True
        except OSError:
            # Worker died mid-write (update/crash). Clear it so the main loop
            # respawns, then retry the same line on the next worker.
            with runtime.cond:
                if runtime.proc is proc:
                    runtime.proc = None
                    runtime.cond.notify_all()


def _ide_stdin_reader(runtime: _Runtime) -> None:
    """Persistent reader: IDE stdin -> current worker stdin.

    MCP stdio is newline-delimited JSON, so the reader parses complete lines: it
    captures the ``initialize`` / ``notifications/initialized`` handshake (replayed
    to respawned workers) and forwards every line in order. If a worker is down it
    holds the line until a replacement spawns.
    """
    buf = b""
    try:
        while not runtime.shutdown:
            try:
                data = os.read(0, _CHUNK)
            except OSError:
                break
            if not data:  # IDE closed the pipe
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                payload = line.rstrip(b"\r")
                if not payload.strip():
                    continue
                _record_handshake(runtime, payload)
                if not _write_line_to_worker(runtime, payload):
                    return
    finally:
        with runtime.cond:
            runtime.stdin_eof = True
            proc = runtime.proc
            runtime.cond.notify_all()
        log.info("IDE stdin EOF — closing worker stdin and waiting for it to exit")
        # Let the (possibly idle) worker see EOF so it shuts down cleanly.
        if proc is not None:
            try:
                stdin = proc.stdin
                if stdin is not None:
                    stdin.close()
            except OSError:
                pass


def _swallow_until_response(fd: int, want_id, timeout: float = 20.0) -> None:
    """Read worker stdout until the JSON-RPC response with id == ``want_id``.

    Consumes a respawned worker's ``initialize`` response so it is NOT forwarded
    to the client (which already received one and is not expecting another).
    Bounded by ``timeout`` so a broken worker cannot wedge the bridge.

    Uses a non-blocking read loop (``os.set_blocking``) rather than
    ``select.select``: on Windows ``select`` only works on sockets, not pipes
    (OSError 10038), which broke handshake replay on every respawned worker.
    """
    buf = b""
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(fd, False)
    except OSError:  # pragma: no cover - non-blocking pipes unsupported
        return
    try:
        while time.monotonic() < deadline:
            try:
                data = os.read(fd, _CHUNK)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError:
                return
            if not data:  # worker closed stdout
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(obj, dict) and obj.get("id") == want_id:
                    return
    finally:
        try:
            os.set_blocking(fd, True)  # restore for the stdout-forwarding thread
        except OSError:  # pragma: no cover
            pass


def _replay_handshake(proc: subprocess.Popen, runtime: _Runtime) -> None:
    """Re-establish the MCP session on a (re)spawned worker (hot-reload fix).

    A real MCP client never re-sends ``initialize`` after the bridge hot-swaps
    the worker underneath it, so a fresh worker would reject every request with
    ``-32602 Invalid request parameters`` (confirmed against a live deploy). We
    replay the client's captured ``initialize`` request, swallow the worker's
    response, then send the captured ``notifications/initialized`` — before any
    new client traffic is forwarded.
    """
    with runtime.cond:
        init_req = runtime.init_request
        init_req_id = runtime.init_request_id
        init_notif = runtime.init_notification
    if not init_req:
        return  # no handshake captured yet (first spawn / plain client)
    stdin = proc.stdin
    stdout = proc.stdout
    if stdin is None or stdout is None:  # pragma: no cover - spawn always pipes
        return
    try:
        os.write(stdin.fileno(), init_req)
        _swallow_until_response(stdout.fileno(), init_req_id)
        if init_notif:
            os.write(stdin.fileno(), init_notif)
        log.info("replayed MCP initialize handshake to respawned worker")
    except OSError:
        log.warning("replay failed: worker pipe write/read error")


def _worker_stdout_to_ide(worker: subprocess.Popen) -> None:
    """Forward worker stdout -> IDE stdout (fd 1). One thread per worker."""
    stdout = worker.stdout
    if stdout is None:  # pragma: no cover - spawn always pipes stdout
        return
    try:
        rfd = stdout.fileno()
        while True:
            try:
                data = os.read(rfd, _CHUNK)
            except OSError:
                return
            if not data:  # worker closed stdout (exited)
                return
            _write_all(1, data)
    except (OSError, ValueError, AttributeError):
        pass


# ══════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════


def run_bridge() -> int:
    """Run the persistent bridge until the IDE disconnects or we are signalled."""
    try:
        worker_cmd = resolve_worker_cmd()
    except WorkerUnavailableError as exc:
        log.error(f"bridge cannot start: {exc}")
        return 1

    lock = default_lock_path()
    log.info(f"bridge starting (pid={os.getpid()} parent={os.getppid()}) worker={' '.join(worker_cmd)} lock={lock}")

    runtime = _Runtime()
    signal.signal(signal.SIGINT, _make_signal_handler(runtime))
    signal.signal(signal.SIGTERM, _make_signal_handler(runtime))

    # Parent (bootloader / host launcher) watchdog: the host manages the process
    # it launched — for a PyInstaller ONEFILE that is this bridge's PARENT, not
    # this process. If it dies (VS Code stop / kill / close) without a clean EOF,
    # self-terminate and take the worker tree with us (orphan prevention).
    def _on_parent_death() -> None:
        log.warning("bridge parent (bootloader/host) died — tearing down worker tree and exiting")
        with runtime.cond:
            runtime.shutdown = True
            proc = runtime.proc
            runtime.cond.notify_all()
        if proc is not None:
            _terminate_worker_tree(proc)
        os._exit(0)  # job object / killpg already reaped the worker tree

    watch_parent(os.getppid(), _on_parent_death)

    reader = threading.Thread(
        target=_ide_stdin_reader,
        args=(runtime,),
        name="bridge-stdin-reader",
        daemon=True,
    )
    reader.start()

    generation = 0
    try:
        while not runtime.shutdown:
            # Wait out any in-flight publish before (re)spawning a worker. This also
            # covers the very first spawn when the bridge boots mid-update.
            wait_for_lock_clear(lock)

            with runtime.cond:
                if runtime.stdin_eof or runtime.shutdown:
                    break

            generation += 1
            log.info(f"spawning worker (gen {generation})")
            try:
                proc = spawn_worker(worker_cmd)
            except Exception as exc:  # noqa: BLE001 — surface any spawn failure clearly
                log.error(f"failed to spawn worker: {exc}")
                return 1
            log.info(f"worker (gen {generation}) spawned pid={proc.pid}")
            # Windows: kill-on-close Job Object so the worker tree dies with this
            # bridge even on a force-kill (no cleanup code runs then).
            _assign_worker_tree_kill(proc)

            # Re-establish the MCP session on the fresh worker BEFORE routing any
            # new client traffic (the reader holds lines while runtime.proc is
            # still None, so the replayed initialize precedes them).
            _replay_handshake(proc, runtime)

            with runtime.cond:
                runtime.proc = proc
                runtime.cond.notify_all()

            fwd = threading.Thread(
                target=_worker_stdout_to_ide,
                args=(proc,),
                name=f"bridge-stdout-fwd-{generation}",
                daemon=True,
            )
            fwd.start()

            # Wait for the worker to exit — bounded once we are shutting down, so
            # a worker that ignores stdin EOF can never wedge the bridge forever.
            rc = None
            eof = False
            eof_deadline = None
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                with runtime.cond:
                    eof = runtime.stdin_eof or runtime.shutdown
                if eof:
                    if eof_deadline is None:
                        eof_deadline = time.monotonic() + EOF_GRACE_SECONDS
                        log.info("EOF/shutdown — giving the worker a short grace to exit")
                    if time.monotonic() >= eof_deadline:
                        log.warning("worker did not exit after EOF — force-killing worker tree")
                        _terminate_worker_tree(proc)
                        rc = proc.wait()
                        break
                time.sleep(0.1)
            with runtime.cond:
                if runtime.proc is proc:
                    runtime.proc = None
                runtime.cond.notify_all()

            log.info(f"worker (gen {generation}) exited rc={rc}")
            if eof:
                break  # IDE disconnected or we were signalled — do not respawn
            # Worker exited on its own (crash, or a publish just killed it). A
            # small settle prevents a tight loop; the next iteration waits on the
            # lock and respawns the (possibly fresh) worker.
            time.sleep(CRASH_BACKOFF_SECONDS)
    finally:
        # Every exit path (signal, EOF, parent death, exception): ensure the
        # worker tree is gone so nothing is left orphaned.
        with runtime.cond:
            remaining = runtime.proc
        if remaining is not None and remaining.poll() is None:
            _terminate_worker_tree(remaining)

    log.info("bridge exiting")
    return 0


# ══════════════════════════════════════════════════════════════════════════
#  Subcommand passthrough (2.1 - 2.4)
# ══════════════════════════════════════════════════════════════════════════

# Routing rule: an IDE stdio MCP session launches the bridge with NO arguments
# (that path runs the hot-reload proxy). Any argument means the caller wants a
# one-shot worker invocation — ``hook`` (host automation), ``daemon``, or
# anything else such as ``--version`` / unknown tokens — and is passed through
# by replacing this process with the worker. This preserves the pre-split
# behavior exactly (the old single binary only special-cased hook/daemon and ran
# the MCP server for any other leading argument).


def _exec_worker(argv: list[str]) -> int:
    """Replace this process with the worker, preserving ``argv`` (2.1-2.4).

    ``os.execv`` swaps the process image in place, so a short-lived invocation
    (a hook call that reads stdin, forwards to the daemon, writes stdout and
    exits) costs exactly the same as calling the worker directly — no second
    Python interpreter, no double PyInstaller boot.

    Before exec'ing we wait out any in-flight publish (2.2): if ``.update_lock``
    is present the bridge holds (bounded) until the publisher clears it, so a
    hook can never exec a half-written ``awlab-ai-worker`` binary mid-publish.
    """
    try:
        worker_cmd = resolve_worker_cmd()
    except WorkerUnavailableError as exc:
        log.error(f"cannot pass through '{' '.join(argv)}': {exc}")
        return 1

    # Never exec a half-written worker (no-op stat in steady state).
    wait_for_lock_clear(default_lock_path())

    # New argv = worker invocation + the original subcommand args. argv[0] of the
    # new image is the worker executable; the worker's ``__main__`` reads argv[1]
    # ("hook" / "daemon") exactly as it did before the split.
    new_argv = [*worker_cmd, *argv]
    log.info(f"passing through: {worker_cmd[0]} {' '.join(argv)}")
    try:
        os.execv(worker_cmd[0], new_argv)
    except OSError as exc:
        log.error(f"exec failed for worker {worker_cmd[0]}: {exc}")
        return 1
    return 0  # unreachable — os.execv only returns on failure


# ══════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """Route the bridge invocation.

    * No arguments            -> persistent stdio proxy (the MCP hot-reload
                                 bridge that IDEs launch).
    * Any subcommand          -> ``os.execv`` the worker with the same argv
                                 (``hook`` automation, ``daemon``, ``--version``,
                                 unknown tokens) for zero-overhead passthrough.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        return _exec_worker(argv)
    return run_bridge()


if __name__ == "__main__":
    sys.exit(main())
