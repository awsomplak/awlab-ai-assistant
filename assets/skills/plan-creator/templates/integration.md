---
project_types: [web, mobile, backend]
frameworks: [any]
last_used: 2026-05-18
success_count: 0
---
# Template: API / 3rd Party Integration

## Trigger Keywords
`integrate`, `connect`, `API integration`, `third-party`, `webhook`, `external service`, `SDK`, `plugin`

## Plan Skeleton

### Phase 1: Interface Design
- [ ] Document the external API/service (endpoints, auth, data format)
- [ ] Define the integration interface (wrapper/service class)
- [ ] Design error handling and retry strategy
- [ ] Plan data mapping between systems
- [ ] Identify configuration/secrets needed (API keys, webhooks)

### Phase 2: Implementation
- [ ] Create service/client class with typed methods
- [ ] Implement authentication (API key, OAuth, etc.)
- [ ] Implement core operations (CRUD, sync, webhook handler)
- [ ] Add error handling, logging, and rate limit awareness
- [ ] Store configuration securely (env vars, secrets manager)

### Phase 3: End-to-End Testing
- [ ] Write unit tests with mocked API responses
- [ ] Write integration tests against sandbox/staging API
- [ ] Test error scenarios (timeout, auth failure, rate limit)
- [ ] Verify data consistency between systems
- [ ] Document usage examples and configuration guide
