from __future__ import annotations

import threading
from pathlib import Path

from ...config import settings
from ...helpers.file_utils import AcquireLock


# graphify's ``extract(parallel=True)`` uses a ProcessPoolExecutor. Default OFF:
# sequential is proven faster at realistic project scale (Windows spawn overhead
# exceeds the small parallelizable portion), and the pool hangs in the frozen
# onefile exe. Opt in for very large corpora via the central config
# ``settings.graph_parallel`` (env ``GRAPH_PARALLEL=1`` or config.json).
def _use_parallel() -> bool:
    """Whether graphify extraction should use the ProcessPoolExecutor.

    Single source of truth: ``settings.graph_parallel`` (see ``config.py``) — so
    the toggle lives in one place alongside log-level and other env settings.
    """
    return settings.graph_parallel


# A stale graph whose rebuild would be heavy (first build or many changed files)
# runs in a background thread so the MCP request returns immediately; the current
# graph.json stays readable thanks to graphify's atomic writes. Small incremental
# rebuilds stay synchronous so a read right after an edit returns accurate data.
_BACKGROUND_THRESHOLD = 20  # changed files at/above which rebuild runs in background


_ENABLE_PHP_IMPLEMENTS_ENRICHMENT = True


class _CompositeBuildLock:
    def __init__(self, root: Path):
        self._thread_lock = threading.RLock()
        lock_file = Path(root) / ".ai" / "codegraph" / "build.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._process_lock = AcquireLock(lock_file)

    def acquire(self, timeout: float | None = None) -> bool:
        # threading.RLock acquire takes timeout in seconds (or -1 for infinite)
        t_timeout = timeout if timeout is not None else -1
        if not self._thread_lock.acquire(timeout=t_timeout):
            return False
        if not self._process_lock.acquire(timeout=timeout):
            self._thread_lock.release()
            return False
        return True

    def release(self) -> None:
        self._process_lock.release()
        self._thread_lock.release()

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError("Could not acquire composite build lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


_BUILD_LOCKS: dict[str, _CompositeBuildLock] = {}

_BUILD_LOCKS_GUARD = threading.Lock()


# ── Large-graph HTML viz (enhanced filter + drill-down layer) ──────────────
# The injected client-side layer (path filter, min-degree slider, 2-hop focus,
# community drill-down) makes graph.html usable on projects with thousands of
# nodes. vis.js forceAtlas2 physics freezes the browser above this many visible
# nodes, so the layer disables physics until the user narrows the view.
_VIZ_PHYSICS_LIMIT = 2000

# Per-community member cap embedded into the aggregated view's drill-down
# payload (bounds the HTML size on very large graphs; the UI notes the overflow).
_VIZ_DRILLDOWN_CAP = 1500

# Marker used to make the injected layer idempotent across rebuilds.
_VIZ_MARKER = "awlab-large-viz"

_BACKGROUND_THREADS: dict[str, threading.Thread] = {}

_BACKGROUND_ERRORS: dict[str, str] = {}

# Last per-chunk progress timestamp (time.monotonic) per workspace — the stall
# watchdog uses it to detect a worker that is alive but never finishes a chunk.
_BACKGROUND_PROGRESS: dict[str, float] = {}

# Watchdog threads indexed by workspace key (one per active rebuild).
_WATCHDOG_THREADS: dict[str, threading.Thread] = {}


# A background worker that has not finished a chunk within this window is treated
# as stalled: its failure is surfaced via background_error and it no longer counts
# as "in flight", so subsequent builds are not blocked forever by a zombie thread.
_BACKGROUND_STALL_SECONDS = 600.0

# Bounded wait on the per-project build lock — a genuinely stuck build can never
# block a caller forever; it fails with a clear error instead.
_BUILD_LOCK_TIMEOUT = 600.0


def _build_lock(root: Path) -> _CompositeBuildLock:
    """Return the per-project composite lock (thread + process safety)."""
    key = str(Path(root).resolve())
    with _BUILD_LOCKS_GUARD:
        if key not in _BUILD_LOCKS:
            _BUILD_LOCKS[key] = _CompositeBuildLock(root)
        return _BUILD_LOCKS[key]


# Worker-lifecycle fields persisted into .build_state.json so a server restart
# can detect (and auto-clear) a stale `rebuilding` flag that has no live worker.
_REBUILD_STATE_KEYS = (
    "rebuilding",
    "rebuilding_started_at",
    "rebuilding_last_progress_at",
    "rebuilding_error",
)


def _codegraph_dir(root: Path) -> Path:
    return root / ".ai" / "codegraph"


def _resolve_root(workspace_path: str | Path, root: str | Path | None = None) -> Path:
    """Resolve the scan root: absolute as-is, relative joined to workspace_path.

    A relative ``root`` (e.g. ``"src"``) MUST resolve against ``workspace_path``
    — never the server CWD, which is arbitrary (frozen exe / stdio server).
    Without this, ``graph_build root="src"`` scans the wrong directory (or
    fails with "no supported source files detected").
    """
    base = Path(workspace_path).resolve()
    if root is None:
        return base
    r = Path(root)
    return r.resolve() if r.is_absolute() else (base / r).resolve()


def _resolve_graph_root(
    workspace_path: str | Path,
    root: str | Path | None = None,
    family: str | None = None,
) -> Path:
    """Resolve the graph root for an op, honoring a project family.

    Family ops target the family's MERGED graph directory (config home — works
    across drives, no common ancestor required). Non-family ops target the
    WORKSPACE root's ``.ai/codegraph`` — ``root`` only scopes the scan, never
    the output location (so ``root="src"`` cannot drop ``.ai`` inside ``src/``).
    """
    if family:
        return _family_codegraph_dir(family)
    return _resolve_root(workspace_path)


def _family_codegraph_dir(slug: str) -> Path:
    """Where the merged family graph lives (config home — correct across drives)."""
    return settings.config_home / "codegraph" / f"family_{slug}"
