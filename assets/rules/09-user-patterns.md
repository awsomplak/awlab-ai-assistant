<!-- → authority: 00-meta.md -->
# User Pattern Capture (Trigger Points)

Pattern storage and lifecycle are defined in `10-pattern-lifecycle.md`. This rule only specifies **when** to capture a pattern.

## Capture Triggers

- User explicit statement: “I always/prefer/never …”
- User correction: “Don’t use X, use Y instead.”
- Repetition of a command that differs from a stored workflow (live detection).
- After a `/retrospective`, when user says “remember that workflow”.

## Minimum Information Required

Before calling `mem_create_entities` and `mem_tag_entity`, ensure you have:
- `pattern_type`
- `value`
- `source` (explicit/inferred/corrected)
- Current timestamp

Confidence defaults:
- `explicit` → 0.9
- `corrected` → 0.9 (if user corrects your action)
- `inferred` → 0.4 (ask user to confirm if confidence < 0.7)

## Example Flow

1. User says "I prefer to use `pnpm`."
2. Agent follows `10-pattern-lifecycle.md` conflict resolution and stores via:
   ```xml
   <use_mcp_tool>
   <server_name>awlab-memory</server_name>
   <tool_name>mem_create_entities</tool_name>
   <arguments>
   {"entities":[{"name":"pattern_preference_pnpm","entityType":"pattern","observations":[]}]}
   </arguments>
   </use_mcp_tool>
   <use_mcp_tool>
   <server_name>awlab-memory</server_name>
   <tool_name>mem_tag_entity</tool_name>
   <arguments>
   {"observations":[{"entityName":"pattern_preference_pnpm","contents":["type: preference","value: use pnpm for package management","confidence: 0.9","timestamp: 2026-06-05T10:00:00Z","source: explicit"]}]}
   </arguments>
   </use_mcp_tool>
   ```
