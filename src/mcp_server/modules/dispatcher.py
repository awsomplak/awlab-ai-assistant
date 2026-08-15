"""
action_call / action_help — the 2-tool MCP surface driven by REGISTRY.

Exposes exactly two tools that replace the 36-tool surface:

- ``action_call(action, params)`` — server-owned orchestration:
  resolve → validate params → run preconditions (idempotent) → run pipeline (ordered)
  → handler → ``{success, result, executed, skipped}``. No partial execution.
- ``action_help(action)`` — per-action (or grouped) usage generated from REGISTRY.

The ``action_call`` tool description is generated from the REGISTRY
(``build_tool_description``), so the visible surface never drifts from the registry.

Call ``register_dispatcher(target_mcp)`` from any registration module to expose the
2-tool surface on a FastMCP instance.
"""

import json
from typing import Annotated, Any

from pydantic import Field

from ..helpers.logger import logger
from ..registry import (
    REGISTRY,
    _maybe_await,
    build_help,
    build_tool_description,
    resolve_action,
    run_pipeline,
    run_preconditions,
    validate_params,
)


def _dispatch_error(
    action: str,
    error: str,
    *,
    invalid: list[dict[str, str]] | None = None,
    suggestions: list[str] | None = None,
) -> str:
    """Loud, actionable error payload (valid-actions list + did-you-mean + help pointer)."""
    payload: dict[str, Any] = {"success": False, "action": action, "error": error}
    if invalid:
        payload["invalid"] = invalid
    if suggestions:
        payload["did_you_mean"] = suggestions
    if action not in REGISTRY:
        payload["valid_actions"] = sorted(REGISTRY)
        payload["help"] = 'Use action_help(action="<name>") for usage.'
    return json.dumps(payload)


async def _action_call(
    action: Annotated[str, Field(description="Action name (see action_help for the full list)")],
    params: Annotated[dict[str, Any] | None, Field(description="JSON object of the action's params")] = None,
) -> str:
    """Dispatch an MCP action. The server runs preconditions/pipeline automatically."""
    spec, canonical, suggestions = resolve_action(action)
    if spec is None:
        logger.tool("action_call").info(f"unknown action '{action}'")
        return _dispatch_error(action, f"Unknown action '{action}'.", suggestions=suggestions)

    validated, errors = validate_params(spec, params)
    if errors:
        return _dispatch_error(canonical, "Invalid params.", invalid=errors)

    workspace_path = validated.get("workspace_path", "")
    executed: list[str] = []
    skipped: list[str] = []
    try:
        state, pre_executed, pre_skipped = await run_preconditions(spec, workspace_path, validated)
        executed.extend(pre_executed)
        skipped.extend(pre_skipped)
        executed.extend(await run_pipeline(spec, workspace_path, validated, state))
    except Exception as e:  # noqa: BLE001 — loud, names the failing step
        logger.tool("action_call").error(f"orchestration failed for '{canonical}': {e}")
        return _dispatch_error(canonical, str(e))

    try:
        result = await _maybe_await(spec["handler"], **validated)
    except Exception as e:  # noqa: BLE001
        logger.tool("action_call").error(f"handler '{canonical}' failed: {e}")
        return _dispatch_error(canonical, f"Handler failed: {e}")

    # Baking tick (Phase 4): after every successful action, cheap re-evaluate for that
    # workspace (read → key → count → consistency → confidence → emit). The tick never
    # breaks the action and never calls back into action_call, so there is no recursion.
    if workspace_path:
        try:
            from ..helpers.baking import bake_tick
            from .bake_scheduler import note_workspace

            bake_tick(workspace_path)
            note_workspace(workspace_path)  # async tier knows to re-bake this workspace
        except Exception:  # noqa: BLE001 — baking must never break the action
            logger.tool("action_call").warning(f"baking tick skipped for {workspace_path}")

    return json.dumps(
        {
            "success": True,
            "action": canonical,
            "result": result,
            "executed": executed,
            "skipped": skipped,
        }
    )


async def _action_help(
    action: Annotated[str | None, Field(description="Action name; omit for grouped overview")] = None,
) -> str:
    """Get per-action usage (or all actions grouped by category) from the REGISTRY."""
    return build_help(action)


def register_dispatcher(target_mcp) -> None:
    """Register the 2-tool dispatcher surface on a FastMCP instance.

    The ``action_call`` tool description is generated from the REGISTRY
    (``build_tool_description``), so the visible tool surface never drifts.
    """
    _action_call.__doc__ = build_tool_description()
    _action_help.__doc__ = "Get usage for an action (or all actions grouped by category)."
    target_mcp.tool(name="action_call")(_action_call)
    target_mcp.tool(name="action_help")(_action_help)
