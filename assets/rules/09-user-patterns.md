<!-- → authority: 00-meta.md -->
# User Pattern Capture (Trigger Points)

Pattern storage and lifecycle are defined in `10-pattern-lifecycle.md`. This rule only specifies **when** to capture a pattern.

## Capture Triggers

- User explicit statement: “I always/prefer/never …”
- User correction: “Don’t use X, use Y instead.”
- Repetition of a command that differs from a stored workflow (live detection).
- After a `/retrospective`, when user says “remember that workflow”.

## Observation-Driven Capture (`mem_observe`)

Prefer **observing** signals to directly writing patterns. Each `mem_observe` records a
signal into `.ai/memory-bank/observations.jsonl`; the deterministic baking pipeline turns
recurring signals into candidates (key → count → consistency → confidence) with **no LLM**:

```
action_call(action="mem_observe", params={
  "observations": [{
    "signature": "cmd_pnpm_install", "value": "user always runs pnpm install",
    "source": "behavioral", "stack": "auto"
  }]
})
```

- Observe on the triggers above (explicit, correction, live detection, retrospective).
- Do NOT `mem_write` a pattern until it either (a) is a strong explicit/corrected signal, or
  (b) appears as a baked `pattern_candidate` (see below).
- `source` drives computed confidence (explicit/corrected 0.9 · behavioral 0.6 · inferred 0.4).

## Per-Agent Baking Tiers

The server bakes identically across three tiers sharing ONE store
(`.ai/memory-bank/observations.jsonl` + `baked.json`):

- **Subagent + hooks** (Copilot, Claude Code, Hermes) — spawn the shared `awlab-baker`
  subagent when the delivery marker shows NEW `pattern_candidates`
  (`docs/en/PATTERN_BAKING_PROTOCOL.md`); hooks capture observations with zero LLM cost.
- **Async + inline** (Cline, OpenCode) — the server’s background bake-scheduler and
  per-action tick bake automatically (no LLM); act on `pattern_candidates` yourself.
- Spawn/act ONLY on NEW candidates (tell-once, token-cost control) — see
  `## Delivered Pattern Candidates` below.

## Minimum Information Required

Before calling `mem_write`, ensure you have:
- `pattern_type`
- `value`
- `source` (explicit/inferred/corrected)
- Current timestamp

Confidence defaults:
- `explicit` → 0.9
- `corrected` → 0.9 (if user corrects your action)
- `inferred` → 0.4 (ask user to confirm if confidence < 0.7)

## Delivered Pattern Candidates (act per the gate)

When a response contains `pattern_candidates` (from `ctx_info mode="context"` or `mem_search`),
act on them — they are told ONCE via the delivery marker, never re-told:

- `explicit`/`corrected` candidates (confidence ≥ 0.6) → store as a convention via `mem_write`
  (value + computed confidence + stack tag).
- Lower-tier or conflicting candidates → ask the user to confirm once, then store.
- Do NOT re-ask or re-deliver the same candidate in the same session unless new evidence arrives.

## Example Flow

1. User says "I prefer to use `pnpm`."
2. Agent follows `10-pattern-lifecycle.md` conflict resolution and stores via:
   ```
   action_call(action="mem_write", params={
     "entities": [{"name": "pattern_preference_pnpm", "entityType": "pattern", "observations": []}],
     "observations": [{"entityName": "pattern_preference_pnpm", "contents": [
       "type: preference", "value: use pnpm for package management",
       "confidence: 0.9", "timestamp: 2026-06-05T10:00:00Z", "source: explicit"
     ]}]
   })
   ```
