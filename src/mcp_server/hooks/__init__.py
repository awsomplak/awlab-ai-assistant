"""
Hook mode — unified ``hook --agent`` event handling.

One executable, one wire protocol, four host adapters. The host fires the hook on a
lifecycle event (UserPromptSubmit / pre_llm_call / PostToolUse / Stop / ...), our exe
normalizes the payload into a :class:`HookEvent`, dispatches by event kind (anti-loop),
and serializes the result back to the host's response shape.

This is the transport/adapter layer. The bake-core integration points (capture → store,
bake → re-evaluate, relay → delivery) are defined here and filled by the baking pipeline
phases; until then they degrade gracefully (no-op) and never break the host agent loop.
"""

from .adapters import HOOK_ADAPTERS, kind_for_event, normalize_payload, serialize_output
from .hook_event import EVENT_KINDS, HookEvent

__all__ = [
    "EVENT_KINDS",
    "HookEvent",
    "kind_for_event",
    "HOOK_ADAPTERS",
    "normalize_payload",
    "serialize_output",
]
