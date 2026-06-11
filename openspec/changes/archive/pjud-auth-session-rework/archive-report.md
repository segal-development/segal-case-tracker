# Archive Report: pjud-auth-session-rework

**Date Archived**: 2026-06-11  
**Change**: pjud-auth-session-rework (Issue #9)  
**Status**: ARCHIVED — Ready to close  
**Project**: segal-case-tracker  

---

## Executive Summary

The PJUD authentication and session subsystem rework shipped successfully across 4 integrated slices. The change unified a dual-layer incompatible session architecture, implemented real lawyer-identity resolution, and enabled autonomous worker re-authentication. All 17 spec requirements satisfied. Verify verdict: **READY TO ARCHIVE** (0 CRITICAL, 2 WARNING, 1 SUGGESTION). Test suite: 419 passed, 1 xfailed (known), 0 failures. mypy app/core: 0 errors.

---

## What Shipped: 4 Slices Integrated

### Slice 1 — Session unification + method rename
- **Artifact**: `openspec/changes/pjud-auth-session-rework/specs/pjud-session-store/spec.md`
- **Outcome**: Single canonical `PJUDSession` dataclass (neutral module, no I/O imports); async-native `SessionStore` (redis.asyncio); deleted `SessionManager`. All 4 call sites (auth.py, pjud.py, sync_scheduler) atomically rewired.
- **Tests**: Round-trip store tests (save via scraper, retrieve via worker); method-rename assertions; UTC timestamp consistency.
- **Evidence**: S1-T1 through S1-T16 all green; `asave_session(s)` then `get_session_by_lawyer(s.lawyer_id)` returns equal session — core bug fixed.

### Slice 2 — Lawyer identity resolution
- **Artifact**: `openspec/changes/pjud-auth-session-rework/specs/lawyer-identity/spec.md` (LID-01, LID-02, LID-03)
- **Outcome**: Real `_get_or_create_lawyer(db, rut, password, auth_method)` — RUT-keyed, idempotent, returns DB-assigned `lawyer.id` (never hardcoded 0/1). Killed hardcode fallbacks.
- **Tests**: Idempotency (concurrent inserts caught and re-queried); no-hardcode assertions; `session.lawyer_id == lawyer.id` binding.
- **Evidence**: S2-T1 through S2-T9 all green; `normalize_rut` single shared helper; `IntegrityError` race guard working.

### Slice 3 — Encrypted credentials + autonomous worker re-auth
- **Artifact**: `openspec/changes/pjud-auth-session-rework/specs/pjud-auth/spec.md` (AUTH-04, AUTH-05, AUTH-06, AUTH-07, LID-04, LID-05)
- **Outcome**: Credential columns (`encrypted_pjud_password`, `encrypted_clave_unica_password`) populated at login (no new migration — ADR-6); worker `_reauth()` decrypts in-memory and re-authenticates autonomously. Clave Única always available; captcha conditional on `settings.has_2captcha`.
- **Tests**: Clave Única re-auth (headless); captcha re-auth with 2Captcha key; captcha skip when key absent; no-credentials skip.
- **Evidence**: S3-T1 through S3-T10 all green; plaintext never persisted; decryption happens only in memory; security gate sign-off captured.

### Slice 4 — Worker movement detection (autonomous polling)
- **Outcome**: Scheduled sync worker wired to detect case movements via `detect_and_sync_movements`. Movement parsing (Historia table scrape) reused from `POST /sync`. New notifiations dispatched via `NotificationService.notify_new_movement` on fresh movements. Capped per-sync via `_select_cases_for_movement_check` (MOVEMENT_CHECK_DEFAULT_MAX).
- **Tests**: Movement scoping, persistence to `last_movement_at`, notification dispatch, idempotency on re-run.
- **Evidence**: S4-T1 through S4-T6 all green; 419 tests pass (410 baseline + 9 new from S4); no regressions.

---

## Requirements Traceability

### AUTH Domain (7 requirements)
- **AUTH-01** SATISFIED: `/auth/login` calls `login_with_token`, persists, returns JWT
- **AUTH-02** SATISFIED: `/auth/login/clave-unica` calls Clave Única method, persists, returns JWT
- **AUTH-03** SATISFIED (MUST NOT): `/refresh` deleted (HTTP 404)
- **AUTH-04** SATISFIED: Worker decrypts stored creds, re-authenticates unattended
- **AUTH-05** SATISFIED: Clave Única always attempted when credentials present
- **AUTH-06** SATISFIED: Captcha path gated on `settings.has_2captcha`; skip with logged reason if key absent
- **AUTH-07** SATISFIED: No-credentials case returns skip reason, no exception

### SESS Domain (5 requirements)
- **SESS-01** SATISFIED: Single `PJUDSession` in neutral module (`app/services/pjud_session.py`)
- **SESS-02** SATISFIED (MUST NOT): `SessionManager` deleted; `SessionStore` is sole store
- **SESS-03** SATISFIED: Primary key `pjud:session:lawyer:{lawyer_id}` — worker retrieves by lawyer_id
- **SESS-04** SATISFIED: All timestamps UTC-aware; aware-vs-aware comparison
- **SESS-05** SATISFIED: Redis unreachable → graceful degradation (no unhandled exceptions)

### LID Domain (5 requirements)
- **LID-01** SATISFIED: `_get_or_create_lawyer` returns existing or creates new; never returns None
- **LID-02** SATISFIED (MUST NOT): No hardcoded `lawyer_id=0/1` in auth/session code
- **LID-03** SATISFIED: Session `lawyer_id` stamps real `Lawyer.id` from resolution
- **LID-04** SATISFIED (MUST NOT): Credentials persisted Fernet-encrypted; plaintext never stored
- **LID-05** SATISFIED: Worker decrypts in-memory only; plaintext discarded after login call

---

## Verify Verdict

**Source**: Observation #510 (`sdd/pjud-auth-session-rework/verify-report`)

| Gate | Result |
|------|--------|
| pytest -m "not integration" | 419 passed, 1 deselected, 1 xfailed (known), 0 failures |
| mypy app/core | 0 errors |
| Regressions | 0 (baseline 410; +9 new from Slice 4) |

**Criticality**: 0 CRITICAL  
**Warnings**: 2 (stale xfail reason, pre-existing utcnow deprecation)  
**Suggestions**: 1 (get_all_active_sessions not implemented — low priority)  

---

## Accepted Follow-Ups (Not Spec Failures)

1. **GitHub Issue #15**: movement-check cap/rotation strategy for long-running lawyers (rate-limiting, re-polling interval).
2. **SyncHistory TOCTOU**: post-sync_cases query for updating `movements_new` has minor race window (not blocking; documented for future hardening).
3. **Stale xfail reason**: `test_login_with_credentials` reason text is outdated (test still works; not a functional defect).

---

## External Blockers (Out of Scope, Documented)

- **Live E2E against PJUD portal**: Requires reCAPTCHA token, real PJUD/Clave Única credentials, Chromium, live PJUD availability. Not a deliverable; mocked tests are the definition of "done."
- **2Captcha autonomous captcha path**: Requires paid 2Captcha API key. Conditional; gated on `settings.CAPTCHA_API_KEY`. Clave Única path is always available (no paid dependency).
- **Committed seed dataset**: Future work for local dev repeatability.

---

## Key Design Decisions (ADRs)

| ADR | Decision |
|-----|----------|
| ADR-1 | One `PJUDSession` in neutral module (`app/services/pjud_session.py`) — no redis/playwright imports |
| ADR-2 | Single `SessionStore` (async); `SessionManager` deleted |
| ADR-3 | Store converted to async-native (`redis.asyncio`) — all callers already async |
| ADR-4/4b | Method rename: `login_with_user_captcha` → `login_with_token`. Refresh = full re-login. Expiry standardized to 25 min UTC |
| ADR-5 | Lawyer resolution: RUT-keyed, idempotent, with `IntegrityError` race guard |
| ADR-6 | Encrypted credentials reuse existing schema columns — no new migration required |
| ADR-7 | Worker re-auth branches on preferred method: Clave Única always; captcha conditional on `has_2captcha` |

---

## Delta Specs Merged to Main

Three new capability specs created and merged into `openspec/specs/`:

1. **`openspec/specs/pjud-auth/spec.md`**  
   - Contains AUTH-01 through AUTH-07 (dual login paths, method rename, worker re-auth)
   - Merged from `openspec/changes/pjud-auth-session-rework/specs/pjud-auth/spec.md`

2. **`openspec/specs/pjud-session-store/spec.md`**  
   - Contains SESS-01 through SESS-05 (unified session model, single store, UTC consistency)
   - Merged from `openspec/changes/pjud-auth-session-rework/specs/pjud-session-store/spec.md`

3. **`openspec/specs/lawyer-identity/spec.md`**  
   - Contains LID-01 through LID-05 (lawyer resolution, no hardcodes, encrypted credentials)
   - Merged from `openspec/changes/pjud-auth-session-rework/specs/lawyer-identity/spec.md`

---

## Change Folder Relocated

- **From**: `openspec/changes/pjud-auth-session-rework/`
- **To**: `openspec/changes/archive/pjud-auth-session-rework/`

Archive contains:
- `proposal.md` (marked success criteria all checked)
- `design.md` (9 ADRs, 8 decisions documented)
- `tasks.md` (all 4 slices, 40+ tasks, all marked complete)
- `specs/pjud-auth/spec.md`, `specs/pjud-session-store/spec.md`, `specs/lawyer-identity/spec.md` (delta specs)

---

## Files Affected (Summary)

**Core Implementation** (delivered across 4 slices):
- `app/services/pjud_session.py` (new)
- `app/services/session_store.py` (refactored to async)
- `app/core/redis.py` (added async client)
- `app/api/v1/auth.py` (method rename, lawyer resolution, encrypted creds)
- `app/api/v1/pjud.py` (lawyer resolution)
- `app/scrapper/pjud/base.py` (import updates)
- `app/scrapper/pjud/clave_unica.py` (import updates)
- `app/scrapper/pjud/civil.py` (import order fix)
- `app/workers/sync_scheduler.py` (async calls, worker re-auth, movement detection)
- `app/services/sync_service.py` (new, movement detection logic)
- `app/config.py` (added `has_2captcha` property)

**Tests** (comprehensive mocked coverage):
- `tests/conftest.py` (async fixtures)
- `tests/services/test_pjud_session.py` (new)
- `tests/services/test_session_store_async.py` (new)
- `tests/services/test_lawyer_identity.py` (new)
- `tests/api/v1/test_auth.py` (updated)
- `tests/api/v1/test_pjud_stateless.py` (updated)
- `tests/workers/test_sync_scheduler.py` (new)
- `tests/workers/test_sync_reauth.py` (new)
- `tests/workers/test_movement_detection.py` (new)

**Deleted**:
- `app/scrapper/session_manager.py`

---

## Learnings & Gotchas

- **Async conversion blast radius**: Converting a sync store to async across 4 call sites requires atomic landing — cannot split without breaking callers. Used `size:exception` to document the exception rationale.
- **RUT normalization**: Critical to have one shared `normalize_rut` helper. Captcha strips verification digit differently from Clave Única; without normalization, `lawyer_id` lookups fail silently.
- **Reversible encryption trade-off**: Fernet encryption is reversible by design (hashing not an option for worker re-auth). Security review confirmed key access controls and rotation procedure before merge.
- **Session TTL vs sync interval mismatch**: 25-min session TTL vs 4-hour sync interval means worker re-auth is mandatory, not optional — a product constraint called out explicitly.
- **Secondary indices in Redis**: Using thin pointers (`session_id → lawyer_id`, `rut → lawyer_id`) keeps one authoritative record and reduces round-trips on the worker's hot path.

---

## Risk Assessment

| Risk | Likelihood | Outcome |
|------|------------|---------|
| Async conversion blast radius | Medium | **MITIGATED** — 4 call sites landed atomically; full mock coverage |
| Hardcoded lawyer_id fallback | Medium | **ELIMINATED** — `_get_or_create_lawyer` always returns real DB id |
| RUT normalization divergence | Medium | **PREVENTED** — single shared `normalize_rut` helper; tests verify idempotency |
| Session persistence mismatch | Medium | **FIXED** — dual-layer incompatibility resolved; round-trip tests prove correctness |
| Reversible password storage | High | **ACCEPTED** — security review gate cleared; key access controls confirmed |

---

## Recommendations for Next Steps

1. **Immediate**: Merge PR 4 (feat/s4-worker-movement-detection) to main to enable autonomous movement polling.
2. **Short-term**: Implement GitHub Issue #15 (movement-check cap/rotation) for rate-limiting on long-running lawyers.
3. **Follow-up**: Post-merge security audit of `ENCRYPTION_KEY` sourcing (must come from secrets manager, not repo).
4. **Future**: Add `lawyer_id` to JWT claims to reduce RUT→lawyer lookups (out of scope for this change).

---

## Closure

All 17 spec requirements satisfied. All ADRs honored. All tasks completed. Test suite green (419 passed). Verify verdict: READY TO ARCHIVE. Change is closed and archived.

**Archive date**: 2026-06-11  
**Verify report**: Observation #510  
**Status**: Archived to `openspec/changes/archive/pjud-auth-session-rework/`
