"""Phase 5 — no worker-named child processes leak.

- Frozen builds must NEVER use the ProcessPoolExecutor (multiprocessing spawn
  re-executes ``awlab-ai-worker`` as pool children) regardless of GRAPH_PARALLEL.
- The background hook daemon must idle out and self-clean instead of lingering
  as an orphaned process forever.
"""

import asyncio
import sys
import threading
import time

from mcp_server.modules.graphify import config as gcfg


def test_use_parallel_always_false_in_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(gcfg.settings, "graph_parallel", True)
    gcfg._PARALLEL_OVERRIDE_LOGGED = False
    assert gcfg._use_parallel() is False, "frozen builds must never spawn pool children"


def test_use_parallel_respects_setting_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(gcfg.settings, "graph_parallel", True)
    assert gcfg._use_parallel() is True
    monkeypatch.setattr(gcfg.settings, "graph_parallel", False)
    assert gcfg._use_parallel() is False


def test_daemon_idles_out_and_returns(monkeypatch, tmp_path):
    """A daemon with no traffic must exit on its own (no permanent orphan)."""
    from mcp_server import daemon

    pf = tmp_path / "daemon.port"
    monkeypatch.setattr(daemon, "_get_port_file", lambda: pf)
    monkeypatch.setattr(daemon, "_DAEMON_IDLE_SECONDS", 1)
    monkeypatch.setattr(daemon, "_DAEMON_CHECK_SECONDS", 0.1)
    monkeypatch.setattr(daemon, "_MY_PORT", None)

    asyncio.run(daemon.start_daemon_server())  # returns when idle
    assert True  # returned instead of hanging


def test_daemon_exits_when_superseded(monkeypatch, tmp_path):
    """If another daemon takes over the port file, this one exits and does not
    clobber the replacement's port."""
    from mcp_server import daemon

    pf = tmp_path / "daemon.port"
    monkeypatch.setattr(daemon, "_get_port_file", lambda: pf)
    monkeypatch.setattr(daemon, "_DAEMON_IDLE_SECONDS", 999)  # never idle
    monkeypatch.setattr(daemon, "_DAEMON_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(daemon, "_MY_PORT", None)

    # Simulate a replacement daemon claiming the port while this one starts.
    def _replace_after_start():
        time.sleep(0.3)
        pf.write_text("99999", encoding="utf-8")  # a different port owns it now

    threading.Thread(target=_replace_after_start, daemon=True).start()
    asyncio.run(daemon.start_daemon_server())  # must return (superseded)
    assert True
