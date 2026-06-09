# Proposal: Scraper Architecture Improvements

## Intent

Fix "Target page closed" errors in PJUD scraper caused by persistent browser model. The in-memory `_sessions` dict stores browser references that become stale when workers restart or handle concurrent requests.

Secondary goal: Add Clave Única authentication to eliminate captcha friction for users who have it.

## Scope

### In Scope

- Fresh browser per API request (stateless, reliable)
- Clave Única authentication as alternative to captcha
- Session restoration from Redis cookies (existing)
- Background worker keeps persistent browser (already isolated)

### Out of Scope

- Browser pooling for batch operations (future optimization)
- Chrome extension for captcha (alternative approach)
- Multi-process browser isolation

## Capabilities

### New Capabilities

- `pjud-clave-unica`: Authentication via Clave Única credentials (username/password flow, no captcha)

### Modified Capabilities

- `pjud-base`: Browser lifecycle changes from persistent to fresh-per-request for API calls

## Approach

**Hybrid Model**:

| Context | Browser Strategy | Rationale |
|---------|------------------|-----------|
| API endpoints | Fresh per request | Stateless, no stale refs |
| Background sync | Persistent | Single process, safe |
| Batch detail fetch | Fresh with reuse | Balance reliability/perf |

**Authentication**:

| Method | Users | Friction |
|--------|-------|----------|
| Clave Única | Users with credentials | None (auto re-auth) |
| Captcha | Fallback | Manual token from frontend |

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/scrapper/pjud/base.py` | Modified | `_get_page()` creates fresh context, remove reuse logic |
| `app/scrapper/pjud/clave_unica.py` | New | Clave Única auth flow |
| `app/api/v1/pjud.py` | Modified | Remove `_sessions` dict, stateless requests |
| `app/api/v1/auth.py` | Modified | Add `/login/clave-unica` endpoint |
| `app/scrapper/session_manager.py` | Modified | Store encrypted Clave Única credentials |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Performance regression (2-3s per request) | High | Accept for reliability; batch detail requests |
| Clave Única credential security | Medium | Encrypt with app secret, audit logging |
| PJUD rate limiting | Low | Existing rate limiter handles this |
| Migration breaks existing sessions | Low | Captcha flow remains default; Clave Única opt-in |

## Rollback Plan

1. Feature flag `SCRAPER_FRESH_BROWSER=false` reverts to persistent model
2. Clave Única is additive; removing endpoint doesn't break captcha flow
3. `_sessions` dict code stays (commented) for 2 releases

## Dependencies

- Redis (existing) for session cookies
- Encryption key in settings for Clave Única credentials

## Success Criteria

- [ ] No "Target page closed" errors in production logs (7 days)
- [ ] API requests complete without stale browser exceptions
- [ ] Clave Única login works end-to-end with test account
- [ ] Existing captcha flow still works (regression test)
- [ ] Background worker sync completes without browser errors

## Review Workload Forecast

| Component | Estimated Lines | Risk |
|-----------|-----------------|------|
| Fresh browser refactor | ~200 | Medium |
| Clave Única auth | ~300 | Low |
| API stateless refactor | ~150 | Medium |
| Tests | ~200 | Low |
| **Total** | **~850** | **Medium** |

**400-line budget risk**: High

**Chained PRs recommended**: Yes

### Recommended PR Split

| PR | Scope | Lines | Dependencies |
|----|-------|-------|--------------|
| PR1 | Fresh browser refactor | ~350 | None |
| PR2 | Clave Única authentication | ~300 | PR1 merged |
| PR3 | Performance tuning (optional) | ~200 | PR2 merged |

**Strategy**: Feature Branch Chain. `feature/scraper-improvements` as base, child branches for each PR.
