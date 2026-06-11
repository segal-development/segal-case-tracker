# Proposal: PJUD Auth + Session Subsystem Rework (Issue #9)

## Intent

PJUD authentication and the scheduled sync worker are broken end-to-end. Login fails because `auth.py` calls a non-existent `login_with_user_captcha` (real method is `PJUDBaseScraper.login_with_token`). Even if login worked, a DUAL incompatible session layer means the worker never finds scraper-created sessions: the scraper saves to `SessionManager` keyed by RUT, while the worker reads `SessionStore` keyed by `lawyer_id`, using two distinct `PJUDSession` dataclasses. Plus `lawyer_id` is hardcoded (`pjud.py:191` = 0, `auth.py` clave-única = 1) because `_get_or_create_lawyer` is a TODO. Result: no live crawl, only static pre-seeded data. Fix the code to be CORRECT and COHERENT so authenticated sessions flow login → store → worker → scrape → persist.

## Scope

### In Scope
- Make both login paths correct: captcha (`/auth/login` via `login_with_token`) and Clave Única (`/auth/login/clave-unica`).
- Implement real `_get_or_create_lawyer` (SQLAlchemy); kill `lawyer_id=0/1` hardcodes.
- Unify the two `PJUDSession` dataclasses and two stores into ONE session model/store keyed coherently so the worker finds sessions by `lawyer_id`.
- Autonomous worker re-auth on session expiry (~25 min) using credentials encrypted with existing Fernet helpers (`encrypt/decrypt_pjud_password`).
- Method rename `login_with_user_captcha` → `login_with_token`; `refresh` = full re-login.
- Verify via MOCK-based tests (Playwright, Redis, PJUD HTTP) + mypy.

### Out of Scope (Non-Goals)
- Live E2E run against the real PJUD portal locally — BLOCKED by external deps (reCAPTCHA token, real PJUD/Clave Única creds, Chromium). Documented as known blockers, not deliverables.
- Committed seed dataset for local dev (future work).
- 2Captcha is OPTIONAL/conditional (enables autonomous captcha-path sync only when a paid key is configured), not a hard requirement.

## Capabilities

### New Capabilities
- `pjud-auth`: dual login paths, method rename, autonomous worker re-auth.
- `pjud-session-store`: unified session model + single store findable by `lawyer_id`.
- `lawyer-identity`: multi-lawyer resolution + encrypted credential storage.

### Modified Capabilities
- None (existing `pjud-*` scraper specs unchanged at requirement level).

## Approach

1. Rename auth methods to the real scraper API; refresh = re-login.
2. Collapse both session dataclasses/stores into one model with `session_id`, `lawyer_id`, `auth_method`, consistent UTC timestamps; scraper and worker share it.
3. Implement `_get_or_create_lawyer` so sessions bind to the correct lawyer.
4. Add encrypted-credential storage; worker decrypts to re-authenticate unattended (Clave Única always; captcha only with 2Captcha key).
5. Fix side bugs noted in exploration (civil.py import, sync save blocking I/O, expires_at UTC consistency).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/api/auth.py` | Modified | Method rename, real `_get_or_create_lawyer`, kill hardcodes |
| `app/scrapper/pjud/base.py` | Modified | `login_with_token` as canonical entry |
| `app/api/pjud.py` | Modified | Remove `lawyer_id=0` |
| session_manager + session_store | Modified | Unify into one model/store |
| worker / sync_scheduler | Modified | Re-auth on expiry via stored creds |
| `app/core/security.py` | Reused | Fernet encrypt/decrypt creds |
| lawyer model / migration | New | Encrypted credential fields |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Reversible password storage (Fernet) | High | Document explicitly; restrict key access; flag for security review |
| Captcha-vs-autonomous tension | High | Autonomous captcha sync requires paid 2Captcha; treat as optional |
| Session-layer refactor blast radius | Med | One unified model; mock tests + mypy before merge |
| Wrong lawyer binding on resolve | Med | Deterministic `_get_or_create_lawyer` keyed on RUT |

## Rollback Plan

Changes are isolated to auth/session/worker modules behind a feature branch. Revert the branch; the unified session model is additive (new migration can be downgraded). No destructive data changes to existing case records.

## Dependencies

- External (blockers, not deliverables): reCAPTCHA token source, real PJUD/Clave Única credentials, Chromium, Redis, Postgres.
- Optional: 2Captcha paid key for autonomous captcha-path sync.

## First-Slice Suggestion

Smallest valuable PR: **unify the session layer + fix method rename** (one coherent `PJUDSession` model/store + `login_with_token` wiring), proven by mock tests. This unblocks the core login → store → worker lookup path. Subsequent chained PRs: (2) real `_get_or_create_lawyer` + kill hardcodes, (3) encrypted-credential storage + autonomous worker re-auth.

## Success Criteria

- [x] Both login paths call real scraper methods; no missing-method errors.
- [x] One `PJUDSession` model/store; worker finds scraper session by `lawyer_id` in mock tests.
- [x] No `lawyer_id=0/1` hardcodes; `_get_or_create_lawyer` returns real lawyer.
- [x] Worker re-authenticates unattended on expiry (Clave Única path) in mock tests.
- [x] mypy clean; security implication of stored creds documented.
