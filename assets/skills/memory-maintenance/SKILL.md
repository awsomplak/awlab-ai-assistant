---
name: memory-maintenance
description: Prune stale memories, deduplicate entities, and remove outdated user patterns.
---

# Memory Maintenance Skill

## Trigger
Manual: user says "run memory maintenance" or "prune old memories".

## Steps

1. **Deduplicate memories** – (optional, if agents supports content deduplication internally).
2. **Prune stale low‑confidence memories** – use `search_memory(query="type: pattern")` and parse `confidence` + `timestamp` from observations, then remove stale entries with `delete_entities(names=[...])`.
3. **Prune user patterns** – run `list_patterns()`, parse `confidence` and `timestamp`, delete those with `confidence < 0.3` and `timestamp > 30 days` using `delete_entities(names=[...])`.
4. **Report** – "🧹 Maintenance complete. Removed X duplicates, pruned Y stale memories, removed Z stale patterns."

## Implementation Notes
- Use `mem_search` with `query` to retrieve candidate memories.
- For timestamp comparison, use current time from system.
- Use `delete_entities(names=["...", "..."])` to remove stale entries in a single call.
- After deleting old data, run `store_memory(entity_name="Maintenance", observation="Pruned X stale items on {date}", pattern_type="workflow")` to log the maintenance action.

## XML Examples

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
{"names": ["stale_pattern_1", "stale_pattern_2"]}
</arguments>
</use_mcp_tool>