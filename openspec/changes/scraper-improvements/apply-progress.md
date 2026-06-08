# Apply Progress: Scraper Improvements

## PR1: Fresh Browser Refactor

**Status**: Complete
**Mode**: Standard (no strict TDD)

### Completed Tasks

- [x] PR1-T1: Create `app/scrapper/pjud/browser.py` with `BrowserFactory` class skeleton
- [x] PR1-T2: Add `SCRAPER_FRESH_BROWSER` feature flag to `app/config.py`
- [x] PR1-T3: Add `auth_method` field to `PJUDSession` in `app/services/session_store.py`
- [x] PR1-T4: Implement `BrowserFactory.__aenter__` and `__aexit__` lifecycle
- [x] PR1-T5: Implement `BrowserFactory.new_page()` with session restoration
- [x] PR1-T6: Refactor `app/api/v1/pjud.py` to remove `_sessions` dict
- [x] PR1-T7: Unit tests for `BrowserFactory` lifecycle
- [x] PR1-T8: Integration test for stateless endpoint

### Files Changed

| File | Action | What Was Done |
|------|--------|---------------|
| `app/scrapper/pjud/browser.py` | Created | BrowserFactory context manager with lifecycle and session restoration |
| `app/config.py` | Modified | Added SCRAPER_FRESH_BROWSER flag and Firebase/App config fields |
| `app/services/session_store.py` | Created | PJUDSession dataclass with auth_method field, SessionStore class |
| `app/api/v1/pjud.py` | Modified | Removed _sessions dict, use BrowserFactory per request |
| `app/scrapper/pjud/__init__.py` | Modified | Export BrowserFactory |
| `tests/scrapper/pjud/test_browser.py` | Created | 9 unit tests for BrowserFactory |
| `tests/api/v1/test_pjud_stateless.py` | Created | 5 integration tests for stateless endpoints |

### Deviations from Design

None — implementation matches design.

### Issues Found

None.

### Test Results

```
tests/scrapper/pjud/test_browser.py: 9 passed
tests/api/v1/test_pjud_stateless.py: 5 passed
tests/test_pjud_base.py: 20 passed (regression check)
```

### Commits

1. `feat(scraper): add BrowserFactory skeleton and session enhancements` — PR1-T1, T2, T3
2. `feat(scraper): implement BrowserFactory lifecycle and session restoration` — PR1-T4, T5
3. `refactor(api): use BrowserFactory and Redis sessions in PJUD endpoints` — PR1-T6
4. `test(scraper): add unit and integration tests for BrowserFactory` — PR1-T7, T8

### Workload / PR Boundary

- Mode: stacked-to-main PR slice
- Current work unit: PR1 - Fresh Browser Refactor
- Boundary: BrowserFactory creation → endpoint refactor → tests
- Estimated review budget impact: ~350 lines (within 400-line budget)

### Status

8/8 tasks complete. Ready for verify.
