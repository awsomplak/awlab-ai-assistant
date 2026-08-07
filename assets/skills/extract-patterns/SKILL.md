---
name: extract-patterns
description: Scan recent conversation for user preferences, conventions, and workflows, then store them using the pattern lifecycle.
---

# Extract Patterns Skill

## Trigger

User says "learn from this session", "remember my patterns", or automatically after a `/summarize session` call.

## Steps

1. **Retrieve session context** – use `action_call(action="mem_search", params={"query": "session: recent work"})` to retrieve stored session context; otherwise ask the user to provide highlights of the recent conversation.

2. **Identify pattern candidates**:
   - User statements containing "I always…", "I prefer…", "don't use…", "my convention is…", etc.
   - Repeated command sequences (from terminal output, if available in logs).
   - Corrections made by the user (e.g., "No, use `yarn` instead of `npm`").

3. **For each candidate**, follow the conflict resolution defined in `#10-pattern-lifecycle`:
   - Determine `pattern_type` (preference, convention, workflow, anti_pattern).
   - Extract `value` (the core rule).
   - Set `source` to `explicit` if user stated it clearly, otherwise `inferred`.
   - Set `confidence` accordingly (0.9 for explicit, 0.4 for inferred, 0.9 for corrections).
   - Check for existing similar patterns using `action_call(action="mem_search", params={"entity_type": "pattern", "query": "{pattern_type}"})`.
- Resolve conflicts (replace via `mem_remove` + `mem_write`, or ask user first as per `#10-pattern-lifecycle`).

4. **Report** – "Added X new patterns: … (and resolved Y conflicts)."

## Example

User: "I always use single quotes for strings."
→ Agent adds pattern: `type: convention, value: use single quotes for strings, confidence: 0.9, source: explicit`.