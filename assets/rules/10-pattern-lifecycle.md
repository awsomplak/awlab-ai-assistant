<!-- → authority: 00-meta.md -->
# Pattern Lifecycle Management

## Purpose

Manage user‑specific patterns that change over time – handle conflicts, updates, and live detection.

## Computed Confidence & Delivery

- **Confidence is computed, not hand-assigned** — the baking pipeline derives it from observations
  (`frequency × consistency × source weight`); `mem_write` stores it as-is.
- **Delivery is once-per-pattern** — when `ctx_info mode="context"` or `mem_search` returns
  `pattern_candidates`, act per the gate and store via `mem_write` (stack-tagged for portability).
  The delivery marker prevents re-telling.

## Pattern Storage Format

Each pattern is a separate entity with the following structure:

- **Name**: `pattern_{type}_{hash(value)}` (e.g., `pattern_preference_pnpm`)
- **Type**: `pattern`
- **Observations** (single string with metadata):
  ```
  type: preference | convention | workflow | anti_pattern
  value: <the actual rule>
  confidence: <float 0.0-1.0>
  timestamp: <ISO date>
  source: explicit | inferred | corrected
  stack: <framework | language | any>
  ```

## Stack Tagging & Portability

Each baked pattern carries a `stack` tag (framework, else language, else `any`) so it is
portable: `any` patterns apply in every project; stack-specific patterns only surface in
matching ones. When storing via `mem_write`, include the stack tag; when reading
(`mem_search` / `ctx_info mode="context"`), only stack-matching + `any` patterns are
returned (`scope_candidates`).

## Recency-Based Replacement

When two patterns conflict:

1. **Higher computed confidence wins** — confidence is computed by the baking pipeline
   (frequency × consistency × source weight), never hand-assigned.
2. **Newer wins** on equal confidence (compare `timestamp`).
3. **Explicit beats inferred** at equal recency.
4. Ties (both explicit, close in time) → **ask the user**, never guess.

Patterns are stored using `mem_write` (entities + observations in one call):
```
action_call(action="mem_write", params={
  "entities": [{"name": "pattern_preference_pnpm", "entityType": "pattern", "observations": []}],
  "observations": [{"entityName": "pattern_preference_pnpm", "contents": [
    "type: preference", "value: use pnpm for package management",
    "confidence: 0.9", "timestamp: 2026-06-05T10:00:00Z", "source: explicit"
  ]}]
})
```

## Conflict Detection & Resolution

When a **new pattern** is about to be stored:

1. Search for existing patterns using:
   ```
   action_call(action="mem_search", params={"query": "type: preference"})
   ```
2. If found:
   - Compare `timestamp` – new wins if newer.
   - Compare `source` – `explicit` beats `inferred`.
   - If both are explicit and close in time → **ask user** via `ask_followup_question`:
     > "I already have a pattern 'use npm' from yesterday. You just said 'use pnpm'. Which one should I keep? (npm/pnpm/both)"
3. If conflict resolved by replacement:
   - ```
     action_call(action="mem_remove", params={"names": ["pattern_old"]})
     ```
   - Create new pattern entity via `mem_write` (as shown above).
4. If no conflict, create new pattern.

## Live Detection (Workflow Patterns)

After every **successful `execute_command`** (especially commands that are likely to be repeated, like install, test, build), agent should:

1. Extract the command (e.g., `pnpm install`).
2. Search for existing `workflow` patterns.
3. If a stored pattern exists but the executed command differs (e.g., pattern says `npm install`, user ran `pnpm install`), agent asks:
   > "I noticed you used `pnpm install` instead of the stored pattern `npm install`. Should I update your workflow preference?"
4. If user says yes → replace the old workflow pattern with the new one.

## Deprecation & Pruning

- Patterns with `confidence < 0.3` and `timestamp > 30 days` are considered stale.
- The `/memory prune` command (or `memory maintenance` skill) will automatically delete such entities.
- Users can also manually delete a pattern via `/memory reject` (if patterns are stored in the review queue – adjust `review.jsonl` to include them initially as low‑confidence).

## Recency in Retrieval

When retrieving patterns for a task (e.g., before `plan-creator`), sort by `timestamp` descending so the most recent patterns are applied first. If confidence is low, ask for confirmation before applying.

## Example Interaction

User: "I prefer to use `yarn` now, not `npm`."

Agent:
1. Lists patterns via:
   ```
   action_call(action="mem_search", params={"query": "pattern_preference_npm"})
   ```
2. Finds existing `pattern_preference_npm` (confidence 0.9, timestamp yesterday).
3. Asks: "You previously preferred npm. Should I replace it with yarn?"
4. User: "Yes."
5. Agent: Archives old entity with `mem_remove`, stores new preference via `mem_write`.
