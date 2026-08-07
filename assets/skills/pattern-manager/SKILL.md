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

1. Run `action_call(action="mem_search", params={"entity_type": "pattern"})` to get all stored pattern entities.
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
2. Run `action_call(action="mem_remove", params={"names": ["{name}"]})`.
3. Confirm deletion.

## Steps for `deprecate`

1. Run `action_call(action="mem_search", params={"entity_type": "pattern"})` to retrieve all pattern entities.
2. For each entity, parse `confidence` and `timestamp` from observations.
3. If `confidence < 0.3` and `timestamp` older than 30 days, add to deletion list.
4. Ask user: "Found X stale patterns. Delete them?" (Yes/No).
5. If yes, delete each via `mem_remove`.
6. Confirm cleanup.

## Steps for `export`

1. Run `action_call(action="mem_search", params={"entity_type": "pattern"})` to collect all pattern observations as JSON objects.
2. Write to `./.ai/patterns_export.json`.

## Examples

```
action_call(action="mem_search", params={"entity_type": "pattern"})

action_call(action="mem_remove", params={"names": ["pattern_preference_pnpm"]})