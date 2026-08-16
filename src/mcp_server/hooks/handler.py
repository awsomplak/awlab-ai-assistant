"""
Hook handler — anti-loop dispatch + project resolution + bake-core integration points.

Flow per event (anti-loop map):
- prompt  → READ (inject stack-scoped baked patterns) + RELAY (delivery-marker-gated ask)
- tool    → CAPTURE only (append observation, dedup guard) — NEVER inject
- pre_tool→ deviation check vs stored patterns → allow or block (terminal, no loop)
- stop    → BAKE (batch → key→count→consistency→confidence) + update delivery marker
- session / subagent → observer only

The bake-core integration points (capture_observation / bake_project / read_patterns /
relay_candidate) are defined in the baking pipeline phases; this module wires them and
degrades gracefully (no-op) until then.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import settings
from .hook_event import HookEvent

# ── Project resolution + project-id bootstrap ───────────────────────────────


def _sanitize_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "project"


def resolve_project(hook: HookEvent) -> HookEvent:
    """Resolve project_path + project_id for the event.

    Path: already derived by the adapter from the payload (cwd / CLAUDE_PROJECT_DIR /
    workspace URI) or passed via --project. Reuse the existing isolation chain:
    1) .ai/project-id file → 2) sanitized dir-name fallback. Server-side bootstrap:
    write .ai/project-id when missing (hook-only, within project root).
    """
    if not hook.project_path:
        return hook
    root = Path(hook.project_path).resolve()
    try:
        pid = settings.get_project_id(root)
        if not pid:
            # Server-side project-id bootstrap (hook-only): derive + write.
            pid = _sanitize_slug(root.name)
            pid_file = settings.get_project_id_path(root)
            try:
                pid_file.parent.mkdir(parents=True, exist_ok=True)
                pid_file.write_text(pid, encoding="utf-8")
            except OSError:
                pass  # best-effort; fallback scope still works
        hook.project_path = str(root)
        hook.project_id = pid or ""
    except Exception:  # noqa: BLE001 — never break the host loop
        hook.project_path = str(root)
    return hook


# ── Bake-core integration points (filled by baking pipeline phases) ─────────


def capture_observation(hook: HookEvent) -> None:
    """Capture an observation from a tool/prompt event (append-only jsonl).

    Observer-only: writes to the observation store, never injects. Dedup/delta
    guard (fingerprint) prevents double-counting on repeated hook fires.
    """
    if not hook.project_path:
        return
    value = hook.command or hook.user_message
    if not value:
        return
    try:
        from ..helpers.observation_store import append_observations

        append_observations(
            workspace_path=hook.project_path,
            records=[
                {
                    "signature": f"hook_{hook.kind}_{hook.tool_name}" if hook.tool_name else f"hook_{hook.kind}",
                    "value": value,
                    "source": "behavioral",
                    "stack": "any",
                    "project": hook.project_id,
                }
            ],
        )
    except Exception:  # noqa: BLE001 — never break the host loop
        pass


def bake_project(hook: HookEvent) -> dict[str, Any]:
    """Run the bake pipeline (key → count → consistency → confidence) + marker.

    Delegates to the shared ``baking.bake_tick`` so the hook path and the
    ``action_call`` per-call tick produce identical candidates from the same store.
    """
    if not hook.project_path:
        return {"context": "", "candidates": []}
    try:
        from ..helpers.baking import bake_tick

        baked = bake_tick(hook.project_path)
        return {"context": "", "candidates": baked.get("candidates") or []}
    except Exception:  # noqa: BLE001 — never break the host loop
        return {"context": "", "candidates": []}


def read_patterns(hook: HookEvent) -> str:
    """Inject stack-scoped baked patterns (READ side).

    Phase 4 fills this (reuse _scope_patterns). Until then returns "".
    """
    return ""


def relay_candidate(hook: HookEvent, candidates: list[dict[str, Any]]) -> str:
    """Delivery-marker-gated ask for newly baked candidates (gate decides).

    Phase 4 + gate fill this. Until then returns "".
    """
    return ""


# ── Deviation check (terminal block verdict, no loop) ───────────────────────


def _check_deviation(hook: HookEvent) -> str | None:
    """Return a block message if a stored workflow pattern conflicts with the command.

    Phase 3 fills this (compare command signature vs stored workflow patterns).
    Until then returns None (allow).
    """
    return None


# ── Dispatch ────────────────────────────────────────────────────────────────


def handle_hook(hook: HookEvent) -> dict[str, Any]:
    """Dispatch a normalized hook event by kind (anti-loop).

    Returns an internal result dict; the adapter serializes it to the host shape.
    """
    resolve_project(hook)
    result: dict[str, Any] = {"context": "", "candidates": [], "block": None}

    if hook.kind == "prompt":
        # READ + RELAY (only inject point — user-initiated, cannot self-loop).
        context = read_patterns(hook)
        candidates = bake_project(hook).get("candidates") or []
        relay = relay_candidate(hook, candidates)
        pieces = [p for p in (context, relay) if p]
        result["context"] = "\n".join(pieces)
        result["candidates"] = candidates

    elif hook.kind == "tool":
        # CAPTURE only — observer-only, never injects.
        capture_observation(hook)

    elif hook.kind == "pre_tool":
        # Deviation check → terminal block verdict or allow.
        block = _check_deviation(hook)
        result["block"] = block

    elif hook.kind == "stop":
        # BAKE + marker update.
        baked = bake_project(hook)
        result["candidates"] = baked.get("candidates") or []
        result["context"] = baked.get("notify") or ""

    # session / subagent → observer-only, no action yet.

    return result
