"""
Server async bake tick — a background daemon re-evaluates the observation store.

Three-tier orchestration, tier 1: alongside the synchronous inline tick (every
``action_call``) and the subagent tier, a periodic background thread re-bakes known
workspaces so observations written OUTSIDE ``action_call`` (e.g. hook captures) still
get baked + delivered. All tiers share the same ``observations.jsonl`` and the same
``bake_tick``, so candidates are identical regardless of which tier produced them.

Lifecycle: ``start_scheduler()`` (idempotent) is called at server startup; the daemon
sweeps every ``BAKE_INTERVAL_SECONDS``. ``note_workspace()`` is called by the dispatcher
so the scheduler knows which workspaces are active. The scheduler never raises — a failed
sweep is a logged no-op.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..helpers.logger import logger

# Config
BAKE_INTERVAL_SECONDS = 30  # how often the background loop re-bakes known workspaces
BAKE_GRACE_SECONDS = 5  # settle delay before the first sweep

_known: set[str] = set()
_known_guard = threading.Lock()
_scheduler: threading.Thread | None = None
_scheduler_guard = threading.Lock()
_stop = threading.Event()


def note_workspace(workspace_path: str | Path) -> None:
    """Remember a workspace so the async tick re-bakes it (idempotent, cheap)."""
    if not workspace_path:
        return
    with _known_guard:
        _known.add(str(workspace_path))


def known_workspaces() -> list[str]:
    """Snapshot of the known (active) workspaces."""
    with _known_guard:
        return sorted(_known)


def _sweep() -> None:
    """Re-bake every known workspace (never raises)."""
    for ws in known_workspaces():
        try:
            from ..helpers.baking import bake_tick

            bake_tick(ws)
        except Exception:  # noqa: BLE001 — background, never crash the server
            logger.warning(f"async bake tick failed for {ws}")


def start_scheduler(interval: float = BAKE_INTERVAL_SECONDS) -> threading.Thread | None:
    """Start the background bake scheduler (idempotent). Returns the daemon thread."""
    global _scheduler
    with _scheduler_guard:
        if _scheduler is not None and _scheduler.is_alive():
            return _scheduler
        _stop.clear()

        def _loop() -> None:
            time.sleep(BAKE_GRACE_SECONDS)
            while not _stop.is_set():
                try:
                    _sweep()
                except Exception:  # noqa: BLE001
                    pass
                _stop.wait(interval)

        t = threading.Thread(target=_loop, name="awlab-bake-scheduler", daemon=True)
        _scheduler = t
        t.start()
        return t


def stop_scheduler() -> None:
    """Signal the scheduler to stop (test teardown / shutdown)."""
    _stop.set()


def sweep_once() -> None:
    """Run a single sweep synchronously (used by tests + the consistency check)."""
    _sweep()


# Re-export for the dispatcher to keep the surface small.
__all__: list[str] = [
    "BAKE_GRACE_SECONDS",
    "BAKE_INTERVAL_SECONDS",
    "note_workspace",
    "known_workspaces",
    "start_scheduler",
    "stop_scheduler",
    "sweep_once",
]
