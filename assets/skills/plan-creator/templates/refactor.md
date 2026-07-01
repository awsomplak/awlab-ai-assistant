---
project_types: [web, mobile, backend, script, tool]
frameworks: [any]
last_used: 2026-05-18
success_count: 0
---

# Template: Refactor / Cleanup

## Trigger Keywords
`refactor`, `clean up`, `reorganize`, `optimize`, `restructure`, `technical debt`, `code quality`

## Plan Skeleton

### Phase 1: Audit & Plan
- [ ] Identify target code/modules for refactoring
- [ ] Document current behavior and public interfaces
- [ ] Identify existing test coverage (add tests if missing — refactor safely)
- [ ] Define refactoring goals (readability, performance, modularity, etc.)
- [ ] List files to modify with expected changes

### Phase 2: Refactor
- [ ] Apply structural changes (extract, rename, move, simplify)
- [ ] Maintain all existing public interfaces and behavior
- [ ] Update internal references and imports
- [ ] Remove dead code and unused dependencies
- [ ] Ensure consistent naming and coding conventions

### Phase 3: Validate
- [ ] Run full test suite — zero regressions
- [ ] Verify all public APIs and interfaces unchanged
- [ ] Run linter/formatter and fix any new warnings
- [ ] Review diff for unintended changes
- [ ] Update documentation if structure changed significantly
