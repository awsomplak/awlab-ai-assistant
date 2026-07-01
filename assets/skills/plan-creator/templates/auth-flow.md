---
project_types: [web, mobile, backend]
frameworks: [any]
last_used: 2026-05-18
success_count: 0
---
# Template: Authentication Flow

## Trigger Keywords
`authentication`, `login`, `signup`, `register`, `auth`, `session management`, `JWT`, `OAuth`

## Plan Skeleton

### Phase 1: Backend Authentication
- [ ] Set up auth provider/package (Sanctum, Passport, NextAuth, etc.)
- [ ] Create/update User model with auth fields
- [ ] Implement registration endpoint with validation
- [ ] Implement login endpoint with credential verification
- [ ] Implement logout and token/session invalidation
- [ ] Add password reset flow (if required)

### Phase 2: Frontend Authentication
- [ ] Create login page/component
- [ ] Create registration page/component
- [ ] Implement auth state management (store/context/provider)
- [ ] Add authenticated route guards / middleware
- [ ] Handle token storage and refresh (if token-based)
- [ ] Add loading and error states for auth flows

### Phase 3: Security Hardening
- [ ] Add rate limiting to auth endpoints
- [ ] Implement CSRF protection (if session-based)
- [ ] Add input sanitization and brute-force protection
- [ ] Write auth integration tests (login, logout, protected routes)
- [ ] Review and verify secure defaults
