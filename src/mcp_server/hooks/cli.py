"""
``hook`` CLI mode — the unified entry for host event hooks.

Usage (called by host hook systems):
    awlab-ai-assistant.exe hook --agent <copilot|claude|cline|hermes> --event <event> \
        [--project <path>]

Reads the host event JSON from stdin, normalizes to a :class:`HookEvent`, dispatches
(anti-loop), serializes the result to the host response shape, writes it to stdout.

Exit codes:
    0  — handled (stdout may carry a response)
    2  — BLOCK decision (hosts that use exit-2-for-block)
    1  — hard error (observer no-op; never crash the host loop)
"""

from __future__ import annotations

import argparse
import json
import sys

from ..helpers.logger import logger
from .adapters import normalize_payload, serialize_output
from .handler import handle_hook


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="awlab-ai-assistant hook", description="Unified host hook handler")
    p.add_argument("--agent", required=True, choices=["copilot", "claude", "cline", "hermes"])
    p.add_argument("--event", required=True, help="Host lifecycle event name")
    p.add_argument("--project", default="", help="Project path fallback (hosts without payload context)")
    return p.parse_args(argv)


def run_hook(argv: list[str]) -> int:
    """Entry point for the hook CLI. Returns an exit code."""
    args = _parse_args(argv)

    try:
        raw_text = sys.stdin.read() if not sys.stdin.isatty() else ""
        raw: dict = json.loads(raw_text) if raw_text.strip() else {}
    except Exception as e:  # noqa: BLE001
        logger.tool("hook").warning(f"hook({args.agent}/{args.event}) bad stdin JSON: {e}")
        raw = {}

    # Project fallback (hosts whose payload lacks project context).
    if args.project and not raw.get("cwd"):
        raw["cwd"] = args.project

    hook = normalize_payload(args.agent, args.event, raw)
    if hook is None:
        logger.tool("hook").warning(f"hook unknown agent '{args.agent}'")
        print("{}")
        return 1

    result = handle_hook(hook)
    out = serialize_output(args.agent, args.event, result)
    print(out)

    # BLOCK verdict → exit 2 for hosts that use exit-2-for-block.
    if result.get("block") and args.agent in ("claude", "hermes"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run_hook(sys.argv[1:]))
