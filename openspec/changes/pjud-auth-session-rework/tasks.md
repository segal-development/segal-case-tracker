# Tasks: pjud-auth-session-rework

Generated from spec (#502) and design (#503).
Delivery: 3 chained PRs (stacked-to-main), feature-branch `feat/notifications-pipeline`.
Test runner: `.venv/bin/python -m pytest -m "not integration"`
Type checker: `.venv/bin/python -m mypy <touched modules>`
TDD order: failing test (RED) → implementation (GREEN) → verify.

---

## Slice 1 — Unify session layer + method rename

**Branch**: `feat/s1-session-unification` (base: main)
**Spec refs**: SESS-01, SESS-02, SESS-03, SESS-04, SESS-05, AUTH-01, AUTH-02, AUTH-03
**Design refs**: ADR-1, ADR-2, ADR-3, ADR-4, ADR-4b, side-bugs (civil.py import, UTC expiry)

This slice must land atomically. There are exactly 4 async call sites to flip
(`auth.py` ×2, `pjud.py`, `sync_scheduler.py`); splitting would leave the codebase
half-async. Requires `size:exception` — see Review Workload Forecast.

### Prerequisites

- [x] **S1-T0** `[IMPL]` Add async conftest fixtures to `tests/conftest.py`:
  `fake_redis` (`fakeredis.aioredis.FakeRedis()`), `session_store(fake_redis)`,
  `sample_session` (canonical `PJUDSession` with UTC-aware timestamps,
  `lawyer_id=42`, `auth_method="captcha"`), `mock_scraper` (`AsyncMock`
  patching `BrowserFactory` and `login_with_token`).
  All async fixtures decorated `@pytest.fixture` with `pytest-asyncio` mode.
  _Satisfies_: test infrastructure (design §7).

### PJUDSession canonical model

- [x] **S1-T1** `[TEST — RED]` Write `tests/services/test_pjud_session.py`:
  - `PJUDSession.create()` produces `created_at` and `expires_at` as UTC-aware datetimes.
  - `is_expired()` returns `False` for a freshly created session and `True` when
    `expires_at` is in the past — compares aware vs aware (no timezone mismatch).
  - `to_redis()` serializes datetimes as ISO strings (no `datetime` objects in dict).
  - `from_redis(to_redis(s)) == s` round-trip; tolerates missing `auth_method` key
    (back-compat for sessions cached before the rename).
  _Satisfies_: SESS-01, SESS-04.

- [x] **S1-T2** `[IMPL]` Create `app/services/pjud_session.py` — canonical
  `PJUDSession` dataclass with fields: `session_id`, `lawyer_id`, `rut`, `cookies`,
  `created_at`, `expires_at`, `local_storage`, `last_used_at`, `auth_method`.
  Include `_utcnow()`, `DEFAULT_SESSION_MINUTES = 25`, `create()` classmethod,
  `is_expired()`, `time_until_expiry()`, `to_redis()`, `from_redis()`.
  No redis or playwright imports — neutral module only.
  _Satisfies_: SESS-01, SESS-04.

### Async Redis client

- [x] **S1-T3** `[IMPL]` Add `get_async_redis_client() -> redis.asyncio.Redis` to
  `app/core/redis.py` (lazy singleton, `redis.asyncio.from_url`, `decode_responses=True`).
  _Satisfies_: ADR-3 prerequisite.

### Async SessionStore

- [x] **S1-T4** `[TEST — RED]` Write `tests/services/test_session_store_async.py`:
  - `asave_session(s)` then `get_session_by_lawyer(s.lawyer_id)` returns an equal
    session — verifies the root-cause bug is fixed (no longer RUT-keyed / lawyer-keyed split).
  - Primary key is `pjud:session:lawyer:{lawyer_id}`.
  - Secondary `get_session_by_id(session_id)` resolves via `pjud:session:id:{session_id}`.
  - Secondary `get_session_by_rut(rut)` resolves via `pjud:session:rut:{rut_clean}`.
  - TTL is derived from `time_until_expiry()` (not a hardcoded constant).
  - Redis unreachable on `asave_session` → no unhandled exception propagates; method
    returns gracefully.
  - Redis unreachable on `get_session_by_lawyer` → returns `None`; no unhandled exception.
  _Satisfies_: SESS-02, SESS-03, SESS-05.

- [x] **S1-T5** `[IMPL]` Refactor `app/services/session_store.py`:
  - Replace local `PJUDSession` dataclass with `from app.services.pjud_session import PJUDSession`.
  - Replace sync redis client with async: `get_async_redis_client()`.
  - Convert all store methods to `async def`:
    `asave_session`, `get_session_by_lawyer`, `get_session_by_id`,
    `get_session_by_rut`, `get_all_active_sessions`, `delete_session`.
  - Implement new key strategy: primary `pjud:session:lawyer:{id}` (full JSON);
    secondary `pjud:session:id:{session_id}` → `lawyer_id`;
    secondary `pjud:session:rut:{rut_clean}` → `lawyer_id`.
    Validity-cache prefix `pjud:session_valid:{session_id}` unchanged.
  - TTL from `session.time_until_expiry().total_seconds()`.
  - Remove old `LAWYER_SESSIONS_PREFIX` indirection; remove `save_session` (sync) alias.
  - Retain `is_session_valid_cached`, `cache_session_validity`,
    `invalidate_session_cache` (existing test coverage still applies; convert
    backing calls to async).
  _Satisfies_: SESS-02, SESS-03, SESS-05, ADR-2, ADR-3.

### Login endpoint rewiring

- [x] **S1-T6** `[TEST — RED]` Update `tests/api/v1/test_auth.py`:
  - `/auth/login` with mocked `login_with_token` (not `login_with_user_captcha`):
    asserts `login_with_token` was awaited with correct args; asserts
    `login_with_user_captcha` symbol does not exist in the `auth` module
    (`hasattr` or `importlib` guard); asserts `store.asave_session` awaited.
  - `/auth/login` with missing `captcha_token` → HTTP 422.
  - `/auth/login` with scraper raising `LoginError` → HTTP 401.
  - `/auth/login/clave-unica` with mocked `ClaveUnicaAuth.login`:
    asserts `store.asave_session` awaited (not sync `save_session`);
    `session_id` present in response.
  - `/auth/login/clave-unica` with `ClaveUnicaAuthError` → HTTP 401.
  - `/auth/refresh` endpoint no longer exists (HTTP 404 or route not registered).
  _Satisfies_: AUTH-01, AUTH-02, AUTH-03.

- [x] **S1-T7** `[IMPL]` Rewrite `app/api/v1/auth.py` login wiring:
  - Replace `from app.scrapper.session_manager import SessionManager, PJUDSession`
    with `from app.services.pjud_session import PJUDSession`.
  - `/auth/login`: call `scraper.login_with_token(rut, password, captcha_token)`;
    build `PJUDSession.create(session_id=str(uuid4()), lawyer_id=<stub 0>,
    rut=rut, cookies=..., auth_method="captcha")`; `await store.asave_session(session)`.
    (Note: `lawyer_id` placeholder 0 is eliminated in slice 2; kept here to keep
    the slice atomic.)
  - `/auth/login/clave-unica`: `await store.asave_session(pjud_session)`; remove
    `lawyer_id = lawyer.id if lawyer else 1` fallback (use 0 as placeholder).
  - Delete `/auth/refresh` endpoint entirely (AUTH-03: MUST NOT exist).
  - `/auth/session-status`: resolve session via `await store.get_session_by_rut(rut)`.
  - `/auth/logout`: `await store.delete_session_by_rut(rut)`.
  _Satisfies_: AUTH-01, AUTH-02, AUTH-03.

### pjud.py and sync_scheduler call sites

- [x] **S1-T8** `[TEST — RED]` Update `tests/api/v1/test_pjud_stateless.py`:
  - `/pjud/login` with mocked scraper: asserts `store.asave_session` awaited;
    asserts `PJUDSession.create()` used (uuid4 `session_id`, not manual timestamp string).
  _Satisfies_: SESS-02 (async store, canonical factory).

- [x] **S1-T9** `[IMPL]` Rewire `app/api/v1/pjud.py` `/pjud/login`:
  - Import `PJUDSession` from `app.services.pjud_session`.
  - Use `PJUDSession.create(session_id=str(uuid4()), ...)` factory.
  - `await store.asave_session(redis_session)`.
  - `lawyer_id` stays at 0 (slice 2 wires the real value).
  _Satisfies_: SESS-02, ADR-3 (call site).

- [x] **S1-T10** `[TEST — RED]` Write `tests/workers/test_sync_scheduler.py`:
  - `sync_lawyer_cases` calls `await store.get_session_by_lawyer(lawyer_id)`.
  - When store returns `None` → result has `skipped=True, reason="no_session"`.
  _Satisfies_: SESS-03.

- [x] **S1-T11** `[IMPL]` Rewire `app/workers/sync_scheduler.py`:
  - Replace `store.get_session_by_lawyer(lawyer_id)` with
    `await store.get_session_by_lawyer(lawyer_id)` (single `await` change).
  _Satisfies_: ADR-3 (call site).

### Mechanical fixes and deletions

- [x] **S1-T12** `[IMPL]` Move `import asyncio` from `app/scrapper/pjud/civil.py:617`
  to the top-level import block. No logic change.
  _Satisfies_: design side-bug (civil.py import at bottom).

- [x] **S1-T13** `[IMPL]` Update imports in `app/scrapper/pjud/base.py`:
  replace `from app.scrapper.session_manager import SessionManager, PJUDSession`
  with `from app.services.pjud_session import PJUDSession`.
  Remove `SessionManager` instantiation in `__init__` if present.
  _Satisfies_: SESS-02 (no import from deleted module).

- [x] **S1-T14** `[IMPL]` Update import in `app/scrapper/pjud/clave_unica.py`:
  replace `from app.services.session_store import PJUDSession`
  with `from app.services.pjud_session import PJUDSession`.
  _Satisfies_: SESS-02.

- [x] **S1-T15** `[IMPL]` Delete `app/scrapper/session_manager.py`.
  No module in `app/` may import from it after S1-T13 and S1-T14.
  _Satisfies_: SESS-02 (no parallel store implementations).

### Slice 1 verification

- [x] **S1-T16** `[VERIFY]` Run:
  ```
  .venv/bin/python -m pytest -m "not integration" -x
  .venv/bin/python -m mypy app/services/pjud_session.py \
      app/services/session_store.py app/core/redis.py \
      app/api/v1/auth.py app/api/v1/pjud.py \
      app/workers/sync_scheduler.py \
      app/scrapper/pjud/base.py app/scrapper/pjud/clave_unica.py
  ```
  All tests green, mypy clean.
  **Rollback boundary**: revert entire slice 1 PR (the 4 call sites are atomic;
  partial revert breaks async callers).

---

## Slice 2 — Lawyer resolution (kill hardcodes)

**Branch**: `feat/s2-lawyer-resolution` (base: `feat/s1-session-unification`)
**Spec refs**: LID-01, LID-02, LID-03
**Design refs**: ADR-5

### normalize_rut shared helper

- [x] **S2-T1** `[TEST — RED]` Add tests to `tests/test_rut.py`:
  - `normalize_rut("16.021.492-9")` == `normalize_rut("16021492-9")`.
  - Both captcha-style (strips formatting, keeps verif digit) and
    clave_unica-style produce the same output for the same underlying RUT.
  - Output format: digits + hyphen + verif digit, no dots (e.g. `"16021492-9"`).
  _Satisfies_: design R4 (RUT normalization — single shared normalizer).

- [x] **S2-T2** `[IMPL]` Add `normalize_rut(rut: str) -> str` to `app/utils/rut.py`:
  strips dots and spaces; ensures hyphen before verif digit; returns
  canonical form `"{digits}-{verif}"`. This is the single normalizer called
  before any `Lawyer.rut` query or `PJUDSession.rut` assignment.
  _Satisfies_: ADR-5, R4.

### Real _get_or_create_lawyer (identity-only)

- [x] **S2-T3** `[TEST — RED]` Write `tests/services/test_lawyer_identity.py`:
  - `_get_or_create_lawyer(db, rut, password, "captcha")` returns existing lawyer
    when RUT already in DB (no duplicate created — idempotent).
  - Returns new lawyer with a DB-assigned `id` (not 0 or 1) when RUT not in DB.
  - `last_login_at` updated on each call.
  - Concurrent insert: `IntegrityError` caught → re-query returns the existing row.
  - Returned `lawyer.id` is never a hardcoded constant.
  Use in-memory SQLite `db` fixture from `conftest.py`.
  _Satisfies_: LID-01, LID-02, LID-03.

- [x] **S2-T4** `[IMPL]` Implement real `_get_or_create_lawyer(db, rut, password, auth_method)
  -> Lawyer` in `app/api/v1/auth.py`:
  - `normalize_rut(rut)` before query.
  - `db.query(Lawyer).filter(Lawyer.rut == rut_norm).first()`.
  - If `None`: create `Lawyer(rut=rut_norm, name=rut_norm, preferred_auth_method=auth_method, is_active=True)`.
  - Catch `IntegrityError` on concurrent insert → re-query.
  - Update `last_login_at = datetime.now(timezone.utc)`.
  - `db.commit(); db.refresh(lawyer); return lawyer`.
  - Identity-only this slice (no cred columns — those in slice 3).
  _Satisfies_: LID-01, ADR-5.

### Wire db dependency into login handlers

- [x] **S2-T5** `[TEST — RED]` Update `tests/api/v1/test_auth.py`:
  - `/auth/login`: stored `session.lawyer_id` equals the `Lawyer.id` returned by
    `_get_or_create_lawyer` (not 0).
  - `/auth/login/clave-unica`: `session.lawyer_id` not 1 (hardcode killed).
  _Satisfies_: LID-02, LID-03.

- [x] **S2-T6** `[IMPL]` Update `app/api/v1/auth.py` login handlers:
  - Add `db: Session = Depends(get_db)` to `/auth/login` and `/auth/login/clave-unica`.
  - Call real `_get_or_create_lawyer(db, rut, password, auth_method)` before
    `PJUDSession.create(...)`.
  - Pass `lawyer.id` as `lawyer_id` to `PJUDSession.create()`.
  _Satisfies_: LID-02, LID-03, AUTH-01, AUTH-02.

- [x] **S2-T7** `[TEST — RED]` Update `tests/api/v1/test_pjud_stateless.py`:
  - `/pjud/login` stored session `lawyer_id != 0` after real resolution.
  _Satisfies_: LID-02.

- [x] **S2-T8** `[IMPL]` Add `db: Session = Depends(get_db)` to `/pjud/login`;
  call `_get_or_create_lawyer`; pass `lawyer.id` to `PJUDSession.create()`.
  (Import `_get_or_create_lawyer` from `app.api.v1.auth` or extract to
  `app/services/lawyer_service.py` — whichever keeps imports clean.)
  _Satisfies_: LID-02.

### Slice 2 verification

- [x] **S2-T9** `[VERIFY]` Run:
  ```
  .venv/bin/python -m pytest -m "not integration" -x
  .venv/bin/python -m mypy app/utils/rut.py app/api/v1/auth.py app/api/v1/pjud.py
  ```
  All tests green, mypy clean.
  **Rollback boundary**: revert slice 2 PR; slice 1 stays intact (only identity
  resolution and hardcode removal land here).

---

## Slice 3 — Encrypted credentials + autonomous worker re-auth

**Branch**: `feat/s3-worker-reauth` (base: `feat/s2-lawyer-resolution`)
**Spec refs**: AUTH-04, AUTH-05, AUTH-06, AUTH-07, LID-04, LID-05
**Design refs**: ADR-6, ADR-7, side-bug (cases_found)
**Gate**: SECURITY REVIEW must be signed off in PR before merge (see S3-T9).

> No new migration required. `lawyers.encrypted_pjud_password` (migration 001)
> and `clave_unica_rut`, `encrypted_clave_unica_password`, `preferred_auth_method`
> (migration 003) already exist.

### has_2captcha derived setting

- [ ] **S3-T1** `[IMPL]` Add `has_2captcha: bool` as a `@property` (or
  `model_validator`-derived field) to `app/config.py Settings`:
  `return bool(self.CAPTCHA_API_KEY)`.
  Single decision point; no scattered `if key` checks.
  _Satisfies_: ADR-7.

### Encrypted credential persistence

- [ ] **S3-T2** `[TEST — RED]` Extend `tests/services/test_lawyer_identity.py`:
  - After captcha login, `lawyer.encrypted_pjud_password` is set and its value
    is NOT equal to the plaintext password (ciphertext check).
  - After clave_unica login, `lawyer.encrypted_clave_unica_password` set; not plaintext.
  - `decrypt_pjud_password(lawyer.encrypted_pjud_password)` returns the original password.
  Use a test `ENCRYPTION_KEY` injected via `settings` override.
  _Satisfies_: LID-04.

- [ ] **S3-T3** `[IMPL]` Extend `_get_or_create_lawyer` in `app/api/v1/auth.py`
  to populate credential columns:
  - captcha: `lawyer.encrypted_pjud_password = encrypt_pjud_password(password)`.
  - clave_unica: `lawyer.clave_unica_rut = rut_norm`;
    `lawyer.encrypted_clave_unica_password = encrypt_pjud_password(password)`;
    `lawyer.preferred_auth_method = "clave_unica"`.
  Uses `encrypt_pjud_password` / `decrypt_pjud_password` from `app/core/security.py`.
  Plaintext password MUST NOT be written to any persistent field.
  _Satisfies_: LID-04, ADR-5/ADR-6.

### Worker autonomous re-auth

- [ ] **S3-T4** `[TEST — RED]` Write `tests/workers/test_sync_reauth.py`:
  - **Clave Única nominal**: session is `None`; `lawyer.preferred_auth_method = "clave_unica"`;
    encrypted creds present → `_reauth` calls mocked `ClaveUnicaAuth.login` →
    returns a `PJUDSession` → `store.asave_session` called → sync proceeds (not skipped).
    _Satisfies_: AUTH-04, AUTH-05.
  - **Captcha with 2Captcha key**: session `None`; `preferred_auth_method = "captcha"`;
    `settings.has_2captcha = True` → mocked solver returns fake token →
    `login_with_token` called → session stored → sync proceeds.
    _Satisfies_: AUTH-06 (configured path).
  - **Captcha without 2Captcha key**: session `None`; `preferred_auth_method = "captcha"`;
    `settings.has_2captcha = False` → `_reauth` returns `None` with
    `reason = "captcha_no_2captcha_key"` → `sync_lawyer_cases` returns
    `{"skipped": True, "reason": "captcha_no_2captcha_key"}` — no exception raised.
    _Satisfies_: AUTH-06 (not configured path).
  - **No stored credentials**: session `None`; no encrypted fields on lawyer →
    `_reauth` returns `None`; worker logs and returns skipped — no exception.
    _Satisfies_: AUTH-07.

- [ ] **S3-T5** `[IMPL]` Add `async def _reauth(lawyer: Lawyer, store: SessionStore)
  -> tuple[Optional[PJUDSession], Optional[str]]` in `app/workers/sync_scheduler.py`:
  - Branch on `lawyer.preferred_auth_method`:
    - `"clave_unica"`: decrypt `encrypted_clave_unica_password`; call
      `ClaveUnicaAuth().login(page, creds, lawyer.id)` (async, headless);
      `await store.asave_session(session)`; return `(session, None)`.
    - `"captcha"` + `settings.has_2captcha`: solve via 2Captcha client;
      `await scraper.login_with_token(...)`; `await store.asave_session(session)`;
      return `(session, None)`.
    - `"captcha"` + not `settings.has_2captcha`: return `(None, "captcha_no_2captcha_key")`.
    - No creds on lawyer: return `(None, "no_credentials")`.
  _Satisfies_: AUTH-04, AUTH-05, AUTH-06, AUTH-07, ADR-7.

- [ ] **S3-T6** `[IMPL]` Wire `_reauth` into `sync_lawyer_cases`:
  ```python
  pjud_session = await store.get_session_by_lawyer(lawyer_id)
  if pjud_session is None:
      pjud_session, reason = await _reauth(lawyer, store)
      if pjud_session is None:
          logger.warning(f"Skipping lawyer {lawyer_id}: {reason}")
          return {"skipped": True, "reason": reason}
  ```
  Requires fetching the `Lawyer` row from DB before the session check.
  _Satisfies_: AUTH-04, AUTH-05, AUTH-06, AUTH-07.

### cases_found bug fix

- [ ] **S3-T7** `[TEST — RED]` Extend `tests/workers/test_sync_scheduler.py`:
  - When `scraper.get_my_cases` raises an exception, the `SyncHistory` record
    committed to DB has `cases_found == 0` (not unset/null).
  _Satisfies_: design side-bug (cases_found unset in failed-path).

- [ ] **S3-T8** `[IMPL]` Fix `app/workers/sync_scheduler.py` except-block in
  `sync_lawyer_cases`: set `cases_found = 0` on the `SyncHistory` record before
  `sync_record.complete(status="failed", ...)`.
  _Satisfies_: design side-bug.

### Security gate (non-code)

- [ ] **S3-T9** `[GATE]` PR body MUST include a signed-off security checklist before merge:
  - `ENCRYPTION_KEY` sourced from a secret manager (not committed to repo).
  - Key access restricted to worker and API roles only.
  - Key rotation procedure documented.
  - Confirm plaintext never appears in logs, DB, or any cache.
  _Satisfies_: ADR-6 SECURITY REVIEW REQUIRED, R1.

### Slice 3 verification

- [ ] **S3-T10** `[VERIFY]` Run:
  ```
  .venv/bin/python -m pytest -m "not integration" -x
  .venv/bin/python -m mypy app/config.py app/api/v1/auth.py \
      app/workers/sync_scheduler.py
  ```
  All tests green, mypy clean, security gate acknowledged.
  **Rollback boundary**: revert slice 3 PR; slices 1–2 stay intact.

---

## Review Workload Forecast

| Slice | Est. additions | Est. deletions | Total changed | Budget risk |
|-------|---------------|----------------|---------------|-------------|
| S1 — session unification | ~700 | ~300 | ~1 000 | **High** |
| S2 — lawyer resolution | ~360 | ~30 | ~390 | Medium |
| S3 — re-auth + creds | ~350 | ~20 | ~370 | Medium |
| **TOTAL** | **~1 410** | **~350** | **~1 760** | — |

**Chained PRs recommended**: Yes (already planned — 3 PRs).

**400-line budget risk**:
- Slice 1: **High** — `pjud_session.py` (~110 lines), async `SessionStore` rewrite
  (~200 modified from 450-line file), 4 call-site rewirings, 3 new test files,
  deletion of `session_manager.py` (180 lines). Cannot be split without leaving
  callers in a half-async broken state. Requires `size:exception` with rationale:
  *atomic async conversion across 4 call sites*.
- Slice 2: **Medium** — borderline; single-PR is fine if test file stays focused.
- Slice 3: **Medium** — under budget; single-PR.

**Decision needed before apply**: Yes — `size:exception` must be accepted for Slice 1
before `sdd-apply` begins that slice.

---

## Parallel vs sequential dependency map

```
S1-T0 (conftest) ──┐
S1-T1 (test)    ──►│
S1-T2 (impl)    ◄──┘  depends on T0, T1
S1-T3 (redis)   ── independent, can run in parallel with T1/T2
S1-T4 (test)    ── depends on T0, T3
S1-T5 (impl)    ── depends on T2, T3, T4
S1-T6 (test)    ── depends on T5 (store must be async to mock it)
S1-T7 (impl)    ── depends on T5, T6
S1-T8 (test)    ── depends on T5
S1-T9 (impl)    ── depends on T5, T8
S1-T10 (test)   ── depends on T5
S1-T11 (impl)   ── depends on T5, T10
S1-T12–T15     ── independent mechanical; can run in parallel after T2 lands
S1-T16 (verify) ── must be last in slice 1

S2-* all depend on S1-T16 being green
S2-T1/T2 (rut)  ── independent of S2-T3/T4
S2-T3/T4/T5/T6  ── sequential within themselves
S2-T7/T8        ── depends on S2-T4 (imports _get_or_create_lawyer)
S2-T9 (verify)  ── last in slice 2

S3-* all depend on S2-T9 being green
S3-T1 (config)  ── independent; can run first
S3-T2/T3        ── depends on S3-T1 indirectly; identity-tests extend S2 tests
S3-T4/T5/T6     ── sequential (test→impl→wire)
S3-T7/T8        ── independent of T4–T6; can run in parallel
S3-T9 (gate)    ── blocks merge only; code can be complete before sign-off
S3-T10 (verify) ── last in slice 3
```

## Slice 4 — Scheduled worker movement detection (added after Slice 1)

**Why:** The scheduled worker (`app/workers/sync_scheduler.py`) only syncs the case LIST; it never enters the per-case detail page, so movements and `last_movement_at` stay empty and the notification engine never fires autonomously. The PJUD case-detail "Historia" table (Folio / Etapa / Trámite / Desc. Trámite / Fec. Trámite / Foja) is the movement source. The parsing already exists (`get_case_detail` / `_parse_case_detail_html`) and is used by `POST /sync` — Slice 4 is WIRING that into the worker, not new scraping.

> Tasks below are a stub; formalize (test-first, per strict TDD) when reached, after S3-T10 is green.

- [ ] S4-T1 (impl): extract the movement-detection flow from `POST /sync` (`_select_cases_for_movement_check` → `get_case_detail` → `convert_api_movements_to_scraped` → `sync_service.sync_movements`) into a reusable service function, if not already reusable.
- [ ] S4-T2 (test): given a lawyer's synced cases, the worker selects cases for movement check (scoped, not all 2524), fetches detail (mock `get_case_detail`), and persists new Movement rows + sets `last_movement_at`.
- [ ] S4-T3 (impl): call that flow inside `sync_lawyer_cases` after the case-list sync; record `movements_new` in `sync_history`.
- [ ] S4-T4 (test): a new movement triggers a notification dispatch (mock `NotificationService`).
- [ ] S4-T5 (impl): apply movement-check scoping/throttle so each run doesn't scrape every case (rate-limit per PJUD-friendliness; reuse the existing `await asyncio.sleep` delay pattern).
- [ ] S4-T6 (verify): mock-based gate green; `last_movement_at` populated in tests; no live scrape.

**Depends on:** S3-T10 green (needs the autonomous session from Slice 3 to actually run unattended).
**Est. size:** ~300-400 lines, mostly wiring + tests (reuses existing detail-parse + notification code).
