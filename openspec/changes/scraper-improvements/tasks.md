# Tasks: Scraper Architecture Improvements

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~850 (across 3 PRs) |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR1 (~350) → PR2 (~300) → PR3 (~200) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Fresh browser per request | PR1 | Base: main; BrowserFactory + endpoint refactor |
| 2 | Clave Única authentication | PR2 | Base: main (after PR1 merged); auth module + endpoint |
| 3 | Performance tuning | PR3 | Base: main (after PR2 merged); caching + optimizations |

---

## PR1: Fresh Browser Refactor

### Phase 1: Foundation (~100 lines)

- [x] PR1-T1: Create `app/scrapper/pjud/browser.py` with `BrowserFactory` class skeleton
  - Files: `app/scrapper/pjud/browser.py` (new)
  - Lines: ~60
  - Dependencies: None
  - Criteria: Class with `__aenter__`, `__aexit__`, `new_page()` stubs

- [x] PR1-T2: Add `SCRAPER_FRESH_BROWSER` feature flag to `app/config.py`
  - Files: `app/config.py`
  - Lines: ~10
  - Dependencies: None
  - Criteria: Flag defaults to `True`, env var override works

- [x] PR1-T3: Add `auth_method` field to `PJUDSession` in `app/services/session_store.py`
  - Files: `app/services/session_store.py`
  - Lines: ~15
  - Dependencies: None
  - Criteria: Dataclass accepts `auth_method: str = "captcha"`

### Phase 2: Core Implementation (~150 lines)

- [x] PR1-T4: Implement `BrowserFactory.__aenter__` and `__aexit__` lifecycle
  - Files: `app/scrapper/pjud/browser.py`
  - Lines: ~40
  - Dependencies: PR1-T1
  - Criteria: Playwright starts/closes cleanly, no leaks

- [x] PR1-T5: Implement `BrowserFactory.new_page()` with session restoration
  - Files: `app/scrapper/pjud/browser.py`
  - Lines: ~50
  - Dependencies: PR1-T4, PR1-T3
  - Criteria: Cookies from `PJUDSession` injected into page context

- [x] PR1-T6: Refactor `app/api/v1/pjud.py` to remove `_sessions` dict
  - Files: `app/api/v1/pjud.py`
  - Lines: ~60
  - Dependencies: PR1-T5
  - Criteria: Endpoints use `BrowserFactory` context manager, stateless

### Phase 3: Testing (~100 lines)

- [x] PR1-T7: Unit tests for `BrowserFactory` lifecycle
  - Files: `tests/scrapper/pjud/test_browser.py` (new)
  - Lines: ~50
  - Dependencies: PR1-T4
  - Criteria: Mock playwright, verify `__aenter__`/`__aexit__` called

- [x] PR1-T8: Integration test for stateless endpoint
  - Files: `tests/api/v1/test_pjud_stateless.py` (new)
  - Lines: ~50
  - Dependencies: PR1-T6
  - Criteria: Two requests don't share browser state

---

## PR2: Clave Única Authentication

### Phase 1: Foundation (~80 lines)

- [x] PR2-T1: Add Clave Única fields to `Lawyer` model
  - Files: `app/models/lawyer.py`
  - Lines: ~25
  - Dependencies: PR1 merged
  - Criteria: `clave_unica_rut`, `encrypted_clave_unica_password`, `preferred_auth_method` columns

- [x] PR2-T2: Create Alembic migration for Lawyer fields
  - Files: `alembic/versions/003_add_clave_unica_fields.py` (new)
  - Lines: ~30
  - Dependencies: PR2-T1
  - Criteria: Migration runs up/down without errors

- [x] PR2-T3: Create `ClaveUnicaCredentials` dataclass
  - Files: `app/scrapper/pjud/clave_unica.py` (new)
  - Lines: ~25
  - Dependencies: None
  - Criteria: Dataclass with `rut`, `password` fields

### Phase 2: Core Implementation (~150 lines)

- [x] PR2-T4: Implement `ClaveUnicaAuth.login()` method
  - Files: `app/scrapper/pjud/clave_unica.py`
  - Lines: ~80
  - Dependencies: PR2-T3, PR1-T5
  - Criteria: Navigates to Clave Única portal, fills form, handles redirect

- [x] PR2-T5: Add Clave Única selectors to YAML config
  - Files: `app/scrapper/pjud/selectors/clave_unica.yaml` (new)
  - Lines: ~20
  - Dependencies: None
  - Criteria: Selectors for RUT input, password input, submit button

- [x] PR2-T6: Add `/login/clave-unica` endpoint to `app/api/v1/auth.py`
  - Files: `app/api/v1/auth.py`
  - Lines: ~50
  - Dependencies: PR2-T4
  - Criteria: Accepts RUT/password, returns session_id, stores in Redis

### Phase 3: Testing (~70 lines)

- [x] PR2-T7: Unit tests for `ClaveUnicaAuth` form filling
  - Files: `tests/scrapper/pjud/test_clave_unica.py` (new)
  - Lines: ~40
  - Dependencies: PR2-T4
  - Criteria: Mock page, verify correct selectors used

- [x] PR2-T8: Integration test for dual auth detection
  - Files: `tests/api/v1/test_auth.py`
  - Lines: ~30
  - Dependencies: PR2-T6
  - Criteria: Both captcha and Clave Única endpoints work independently

---

## PR3: Performance Tuning

### Phase 1: Analysis (~30 lines)

- [ ] PR3-T1: Add timing instrumentation to `BrowserFactory`
  - Files: `app/scrapper/pjud/browser.py`
  - Lines: ~30
  - Dependencies: PR2 merged
  - Criteria: Log browser startup time, page creation time

### Phase 2: Core Implementation (~120 lines)

- [ ] PR3-T2: Implement warm browser pool for batch operations
  - Files: `app/scrapper/pjud/browser.py`
  - Lines: ~60
  - Dependencies: PR3-T1
  - Criteria: Pool of 2-3 browsers for batch detail fetch

- [ ] PR3-T3: Add Redis caching for PJUD session validation
  - Files: `app/services/session_store.py`
  - Lines: ~40
  - Dependencies: None
  - Criteria: Cache session validity for 5 min, reduce PJUD roundtrips

- [ ] PR3-T4: Parallel case detail fetching in sync worker
  - Files: `app/services/sync_service.py`
  - Lines: ~40
  - Dependencies: PR3-T2
  - Criteria: Fetch up to 3 case details concurrently

### Phase 3: Testing (~50 lines)

- [ ] PR3-T5: Benchmark tests for browser startup
  - Files: `tests/scrapper/pjud/test_performance.py` (new)
  - Lines: ~30
  - Dependencies: PR3-T1
  - Criteria: Assert browser startup < 3s

- [ ] PR3-T6: Test session cache invalidation
  - Files: `tests/services/test_session_store.py`
  - Lines: ~20
  - Dependencies: PR3-T3
  - Criteria: Cache expires after TTL, refresh works

---

## Summary

| PR | Tasks | Est. Lines | Focus |
|----|-------|------------|-------|
| PR1 | 8 | ~350 | Fresh browser lifecycle |
| PR2 | 8 | ~300 | Clave Única auth |
| PR3 | 6 | ~200 | Performance |
| **Total** | **22** | **~850** | |

### Implementation Order

1. PR1 first — establishes `BrowserFactory` foundation
2. PR2 after PR1 merged — builds on stateless model
3. PR3 after PR2 merged — optimizes the established patterns
