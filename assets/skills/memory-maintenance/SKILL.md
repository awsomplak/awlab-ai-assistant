---
name: memory-maintenance
description: Prune stale memories, deduplicate entities, and remove outdated user patterns.
---

# Memory Maintenance Skill

## Trigger
Manual: user says "run memory maintenance" or "prune old memories".

## Steps

1. **Deduplicate memories** – (optional, if agents supports content deduplication internally).
2. **Prune stale low‑confidence memories** – use `action_call(action="mem_search", params={"entity_type": "pattern"})` and parse `confidence` + `timestamp` from observations, then remove stale entries with `action_call(action="mem_remove", params={"names": [...]})`.
3. **Prune user patterns** – run `action_call(action="mem_search", params={"entity_type": "pattern"})`, parse `confidence` and `timestamp`, delete those with `confidence < 0.3` and `timestamp > 30 days` using `mem_remove`.
4. **Report** – "🧹 Maintenance complete. Removed X duplicates, pruned Y stale memories, removed Z stale patterns."

## Implementation Notes
- Use `mem_search` with `query` to retrieve candidate memories.
- For timestamp comparison, use current time from system.
- Use `action_call(action="mem_remove", params={"names": ["...", "..."]})` to remove stale entries in a single call.
- After deleting old data, run `action_call(action="mem_write", params={"observations": [{"entityName": "Maintenance", "contents": ["Pruned X stale items on {date}"]}]})` to log the maintenance action.

## Examples

```
action_call(action="mem_search", params={"entity_type": "pattern"})

action_call(action="mem_remove", params={"names": ["stale_pattern_1", "stale_pattern_2"]})