# Archive Report: scraper-improvements

**Change**: scraper-improvements
**Archived**: 2026-06-09
**Status**: Complete

## Change Summary

Fixed "Target page closed" errors in PJUD scraper by replacing the persistent browser + in-memory `_sessions` model with fresh browser per API request. Sessions are restored from Redis cookies. Added Clave Unica authentication as an alternative to captcha for users who have credentials.

### Key Accomplishments

1. **Fresh Browser Architecture**: BrowserFactory context manager creates clean browser instances per API request, eliminating stale reference issues
2. **Clave Unica Authentication**: Alternative auth flow bypasses captcha entirely using Clave Unica credentials
3. **Performance Optimization**: Browser pool for batch operations, Redis session caching, parallel case detail fetching

## PRs Created

| PR | Scope | Status | Lines |
|----|-------|--------|-------|
| PR1 | Fresh Browser Refactor | Ready for merge | ~350 |
| PR2 | Clave Unica Authentication | Ready for merge | ~300 |
| PR3 | Performance Tuning | Ready for merge | ~200 |

**Total**: ~850 lines across 3 stacked PRs (stacked-to-main strategy)

## Files Changed

### PR1: Fresh Browser Refactor

| File | Action | Description |
|------|--------|-------------|
| `app/scrapper/pjud/browser.py` | Created | BrowserFactory context manager with lifecycle and session restoration |
| `app/config.py` | Modified | SCRAPER_FRESH_BROWSER feature flag |
| `app/services/session_store.py` | Created | PJUDSession dataclass with auth_method, SessionStore class |
| `app/api/v1/pjud.py` | Modified | Removed _sessions dict, stateless with BrowserFactory |
| `app/scrapper/pjud/__init__.py` | Modified | Export BrowserFactory |
| `tests/scrapper/pjud/test_browser.py` | Created | 9 unit tests for BrowserFactory |
| `tests/api/v1/test_pjud_stateless.py` | Created | 5 integration tests for stateless endpoints |

### PR2: Clave Unica Authentication

| File | Action | Description |
|------|--------|-------------|
| `app/models/lawyer.py` | Modified | clave_unica_rut, encrypted_clave_unica_password, preferred_auth_method fields |
| `alembic/versions/003_add_clave_unica_fields.py` | Created | Migration for Lawyer model fields |
| `app/scrapper/pjud/clave_unica.py` | Created | ClaveUnicaCredentials dataclass, ClaveUnicaAuth login flow |
| `app/scrapper/pjud/selectors/clave_unica.yaml` | Created | Selectors for Clave Unica portal |
| `app/api/v1/auth.py` | Modified | /login/clave-unica endpoint |
| `tests/scrapper/pjud/test_clave_unica.py` | Created | Unit tests for ClaveUnicaAuth |
| `tests/api/v1/test_auth.py` | Modified | Integration tests for dual auth |

### PR3: Performance Tuning

| File | Action | Description |
|------|--------|-------------|
| `app/scrapper/pjud/browser.py` | Modified | Timing instrumentation, warm browser pool |
| `app/services/session_store.py` | Modified | Redis caching for session validation |
| `app/services/sync_service.py` | Modified | Parallel case detail fetching |
| `tests/scrapper/pjud/test_performance.py` | Created | Benchmark tests for browser startup |
| `tests/services/test_session_store.py` | Modified | Session cache invalidation tests |

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Browser lifecycle | Fresh-per-request for API, Persistent for worker | Stateless = no stale refs; worker is single-process safe |
| Browser factory location | Base scraper via context manager | Natural fit, clean async with support |
| Session data transfer | Redis cookies (existing infra) | TTL built-in, no migration needed |
| Clave Unica credentials | DB encrypted field on Lawyer model | Simple, existing encryption, not volatile like Redis |
| Auth method detection | Dual endpoints + user preference flag | Cleanest API, explicit choice |

## Lessons Learned

1. **Persistent browser models fail in stateless APIs**: In-memory session dicts become stale when workers restart or handle concurrent requests. Fresh browser per request is the safe default for API contexts.

2. **Context managers for lifecycle**: `async with BrowserFactory()` pattern ensures cleanup even on exceptions. The `__aexit__` method handles cleanup in the correct order (page → context → browser → playwright).

3. **Stacked PRs work well for large changes**: The 850-line change was cleanly split into three reviewable chunks (~350, ~300, ~200), each with its own scope and tests.

4. **Feature flags for rollback**: SCRAPER_FRESH_BROWSER flag allows instant revert without code changes.

5. **Clave Unica selectors may change**: Using YAML-based selector config (same pattern as PJUD selectors) makes updates easier without code changes.

## Task Completion

| PR | Tasks | Completed |
|----|-------|-----------|
| PR1 | 8 | 8/8 ✓ |
| PR2 | 8 | 8/8 ✓ |
| PR3 | 6 | 6/6 ✓ |
| **Total** | **22** | **22/22** ✓ |

## Verification Status

- Build: ✓ Passed
- Tests: 34 passed, 0 failed
- Spec Compliance: 10/10 scenarios compliant
- CRITICAL Issues: Resolved (sync_scheduler.py and sync.py imports fixed)

## Final Status

**SDD CYCLE COMPLETE**

The change has been fully:
- ✅ Proposed
- ✅ Designed
- ✅ Task-planned (22 tasks)
- ✅ Implemented (3 PRs)
- ✅ Verified
- ✅ Archived

Ready for the next change.
