"""Per-process session call counter.

Tracks how many ``action_call`` dispatches this worker process has handled.
The counter resets naturally when the worker process restarts (e.g. the bridge
hot-swaps the worker on publish), which makes it a clean "fresh session" signal
for the agent — no heartbeat, no context-window guessing, no new MCP tool.

The MCP surface stays fixed at 2 tools (``action_call`` / ``action_help``);
this counter is surfaced as a field inside the ``ctx_info`` action response.
"""

_SESSION_CALL_COUNT = 0


def bump_session_call_count() -> int:
    """Increment and return the per-process ``action_call`` counter."""
    global _SESSION_CALL_COUNT
    _SESSION_CALL_COUNT += 1
    return _SESSION_CALL_COUNT


def session_call_count() -> int:
    """Return the number of ``action_call`` dispatches handled by this worker process."""
    return _SESSION_CALL_COUNT
