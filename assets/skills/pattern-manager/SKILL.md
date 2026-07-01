---
name: pattern-manager
description: Manage user patterns: list, delete, deprecate, or export.
---

# Pattern Manager Skill

## Trigger

User says "manage patterns", "list my patterns", "delete pattern X", "deprecate old patterns".

## Supported Commands

- `list` – Show all patterns (type, value, confidence, timestamp).
- `delete <name>` – Delete a specific pattern entity.
- `deprecate` – Find patterns with confidence < 0.3 and older than 30 days, ask for confirmation, then delete.
- `export` – Output all patterns as JSON to `./.ai/patterns_export.json`.

## Steps for `list`

1. Run `list_patterns()` via awlab-memory to get all stored pattern entities.
2. For each entity, parse observations and display:

   ```
   Name: {entity_name}
   Type: {type}
   Value: {value}
   Confidence: {confidence}
   Timestamp: {timestamp}
   Source: {source}
   ```

## Steps for `delete`

1. Confirm with user the exact entity name.
2. Run `delete_entities(names=["{name}"])` via awlab-memory.
3. Confirm deletion.

## Steps for `deprecate`

1. Run `list_patterns()` via awlab-memory to retrieve all pattern entities.
2. For each entity, parse `confidence` and `timestamp` from observations.
3. If `confidence < 0.3` and `timestamp` older than 30 days, add to deletion list.
4. Ask user: "Found X stale patterns. Delete them?" (Yes/No).
5. If yes, delete each via `delete_entities(names=[...])`.
6. Confirm cleanup.

## Steps for `export`

1. Run `list_patterns()` to collect all pattern observations as JSON objects.
2. Write to `./.ai/patterns_export.json`.

## XML Examples for awlab-memory

```xml
<use_mcp_tool>
<server_name>awlab-memory</server_name>
<tool_name>list_patterns</tool_name>
<arguments>
{}
</arguments>
</use_mcp_tool>

<use_mcp_tool>
<server_name>awlab-memory</server_name>
<tool_name>delete_entities</tool_name>
<arguments>
{"names": ["pattern_preference_pnpm"]}
</arguments>
</use_mcp_tool>