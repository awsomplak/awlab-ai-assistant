---
project_types: [backend, web]
frameworks: [any]
last_used: 2026-05-18
success_count: 0
---

# Template: Migration / Schema Update

## Trigger Keywords
`migrate`, `upgrade`, `version bump`, `refactor schema`, `database migration`, `breaking change`, `legacy`, `modernize`

## Plan Skeleton

### Phase 1: Analysis & Backup
- [ ] Audit current state (schema, dependencies, API surface)
- [ ] Identify breaking changes and affected components
- [ ] Create backup/rollback strategy
- [ ] Document migration path with before/after mapping
- [ ] Set up staging/test environment (if applicable)

### Phase 2: Execute Migration
- [ ] Apply schema/structural changes
- [ ] Update dependent code (models, controllers, services)
- [ ] Update configuration files and environment variables
- [ ] Run data migration scripts (if applicable)
- [ ] Update imports, namespaces, or API calls

### Phase 3: Verify & Rollback Safety
- [ ] Run full test suite and fix failures
- [ ] Verify critical user flows end-to-end
- [ ] Confirm rollback procedure works
- [ ] Update documentation to reflect new state
- [ ] Remove deprecated code and migration scaffolding
