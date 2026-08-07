<!-- → authority: 00-meta.md -->
# Meta: Rule Priority & Conflict Resolution

## Master Priority Order

When rules appear to conflict, resolve using this explicit priority order (1 is highest):

1. **Security Constraints** (Never overridden. No secrets in `.ai/`, no path traversal out of project root)
2. **05-environment.md** (Shell/command correctness and environment detection)
3. **02-plan-artifacts.md** (Phase execution logic, task formats, UUID rules)
4. **01-memory-bank.md** (Memory operations via awlab-mcp `mem_*` actions)
5. **03-token-strategies.md** (Optimization, compression, context budgets)
6. **06-project-scanner.md** (Scanning behavior, indexing protocols)
7. **04-commands.md** (Reference tables and shortcuts ONLY — never overrides core rules)

## Single Source of Truth Registry

Each concept maps to exactly ONE authoritative file. Other rules may *reference* these concepts but must never *redefine* them.

- **Memory Operations** → `01-memory-bank.md` (uses awlab-mcp `mem_*` actions)
- **Environment & Commands** → `05-environment.md`
- **Project Scanning & Indexing** → `06-project-scanner.md`
- **Phase Execution & Tasks** → `02-plan-artifacts.md`
- **Token Limits & Budgets** → `03-token-strategies.md`

## Deep Analysis Protocol

When analyzing a complex system, trying to understand unfamiliar code, or feeling "confused", strictly follow this sequence:

1. **WHAT** — What specific question am I trying to answer?
2. **WHERE** — Which 1-3 files contain the answer? (Use `action_call(action="mem_search", params={"entity_type": "pattern"})` for patterns first, then check `patterns.md` Quick Index if still file‑based)
3. **READ** — Read ONLY those specific files (maximum of 3 files at a time). NEVER bulk-read files from search results or directory structure lists.
4. **ANSWER** — Answer the specific question.
5. **NEXT** — What is the next specific question?

**ANTI-PATTERNS**: 
- ❌ Do not attempt to "understand the whole system" before taking action. Answer one question at a time.
- ❌ Eagerly loading/reading all files returned by a `grep_search` or list operation (Incremental Reading Protocol MUST be used).

## Rule Conflict Detection Checklist

When creating, editing, or following rules, verify:
- [ ] Are we redefining behavior that belongs in another file?
- [ ] If referencing another concept, are we using `→ see: {authoritative-file}` instead of copying the logic?
- [ ] Does this action violate a higher-priority rule?