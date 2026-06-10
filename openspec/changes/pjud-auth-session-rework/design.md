# Design: PJUD Auth + Session Subsystem Rework (Issue #9)

Architecture-level HOW for the proposal `sdd/pjud-auth-session-rework/proposal`.
Scope is the login → store → worker → scrape → persist authentication spine.
All decisions are sliceable along the proposal's 3-PR chain.

## 1. Architecture Approach

Pattern: **single canonical session value object + one async-native Redis store**,
with login endpoints as thin adapters that resolve identity (lawyer) before
persisting a session the worker can find. The current dual-layer design is
collapsed, not bridged — an adapter would perpetuate two truths.

Layering (dependency direction, top depends on bottom):

```
API handlers (auth.py, pjud.py)        ─ resolve lawyer, call scraper, persist session
        │
Identity service (_get_or_create_lawyer)  ─ SQLAlchemy, RUT-keyed, idempotent
        │
Scraper (base.login_with_token, clave_unica.login)  ─ produce PJUDSession
        │
SessionStore (async, redis.asyncio)    ─ persist/lookup by lawyer_id (+ secondary)
        │
PJUDSession (canonical dataclass)       ─ neutral value object, no I/O
```

Key inversion: `PJUDSession` moves to a neutral module that has **no dependency
on redis or playwright**, so both the scraper and the store import it without a
cycle. Today `base.py` imports `PJUDSession` from `session_manager` (which also
owns a Redis client class), and `clave_unica.py`/`pjud.py` import a *different*
`PJUDSession` from `session_store`. That is the root of the incompatibility.

## 2. Decision: Unified `PJUDSession` Model

### ADR-1 — One canonical dataclass in a neutral module

**Decision.** Create `app/services/pjud_session.py` holding the single
`PJUDSession`. Both `app/scrapper/...` and `app/services/session_store.py`
import from it. The two existing dataclasses are deleted.

Rejected alternatives:
- *Keep both, add a converter/adapter.* Rejected: two sources of truth remain,
  drift returns, and the worker bug is only masked. The proposal explicitly asks
  to **unify**, not bridge.
- *Make `session_store.PJUDSession` canonical in place.* Rejected on import
  direction: `session_store` owns the Redis client; the scraper must not import
  a Redis-bound module to build a value object. A neutral module is cleaner.

### Canonical fields

```python
# app/services/pjud_session.py
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DEFAULT_SESSION_MINUTES = 25  # PJUD real timeout ~25-30 min (see ADR-4)

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class PJUDSession:
    session_id: str                      # uuid4 str, primary id
    lawyer_id: int                       # DB Lawyer.id — the worker lookup key
    rut: str                             # PJUD RUT (normalized, no dots/hyphen)
    cookies: list[dict[str, Any]]        # Playwright cookies
    created_at: datetime                 # UTC-aware
    expires_at: datetime                 # UTC-aware
    local_storage: str = "{}"            # localStorage JSON
    last_used_at: Optional[datetime] = None
    auth_method: str = "captcha"         # "captcha" | "clave_unica"

    @classmethod
    def create(cls, *, session_id, lawyer_id, rut, cookies,
               local_storage="{}", auth_method="captcha",
               minutes=DEFAULT_SESSION_MINUTES) -> "PJUDSession":
        now = _utcnow()
        return cls(session_id=session_id, lawyer_id=lawyer_id, rut=rut,
                   cookies=cookies, created_at=now,
                   expires_at=now + timedelta(minutes=minutes),
                   local_storage=local_storage, auth_method=auth_method)

    def is_expired(self) -> bool:
        return _utcnow() >= self.expires_at          # aware vs aware (ADR-4)

    def time_until_expiry(self) -> timedelta:
        return self.expires_at - _utcnow()

    def to_redis(self) -> dict:                       # ISO strings for JSON
        d = asdict(self)
        for k in ("created_at", "expires_at", "last_used_at"):
            v = getattr(self, k)
            d[k] = v.isoformat() if v else None
        return d

    @classmethod
    def from_redis(cls, data: dict) -> "PJUDSession":
        for k in ("created_at", "expires_at", "last_used_at"):
            data[k] = datetime.fromisoformat(data[k]) if data.get(k) else None
        data.setdefault("auth_method", "captcha")     # back-compat
        return cls(**data)
```

**Serialization to Redis:** in-memory model uses UTC-aware `datetime`; on the
wire it is `json.dumps(session.to_redis())` (ISO-8601 with offset). This removes
the str-vs-datetime split that made the two old models incompatible.

**Producer/consumer contract:**
- Scraper (`login_with_token`, `ClaveUnicaAuth.login`) returns a `PJUDSession`
  built via `PJUDSession.create(...)`. It carries `lawyer_id` — the endpoint
  sets it from identity resolution *before* the scraper builds it, or the
  endpoint stamps it on the returned object before persisting (see §4).
- Worker reads `store.get_session_by_lawyer(lawyer_id)` and gets the same shape.

## 3. Decision: Single Store + Key Strategy

### ADR-2 — `SessionStore` is the only store; `SessionManager` is deleted

**Decision.** `app/services/session_store.py::SessionStore` becomes the single
store. `app/scrapper/session_manager.py::SessionManager` (RUT-keyed) is deleted;
the scraper no longer owns a store — it returns a value object and the endpoint
persists it. This removes the "scraper writes RUT-keyed / worker reads
lawyer-keyed" split that is the core bug.

### Redis key strategy

| Purpose | Key pattern | Value | Why |
|---|---|---|---|
| Primary (worker path) | `pjud:session:lawyer:{lawyer_id}` | full session JSON | The worker's only lookup is by lawyer_id; make it the primary record |
| Secondary by id | `pjud:session:id:{session_id}` | `{lawyer_id}` | Login returns session_id; status/delete by id |
| Secondary by RUT | `pjud:session:rut:{rut_clean}` | `{lawyer_id}` | `/auth/session-status` and `/auth/logout` only have RUT (JWT `sub`) |
| Validity cache | `pjud:session_valid:{session_id}` | `valid`/`invalid` | Unchanged, 5-min TTL |

All keys share the same TTL derived from `time_until_expiry()`. Writing the full
record under the lawyer key (not an indirection) saves the worker a second round
trip on the hot path. The two secondary keys are thin pointers to `lawyer_id`,
keeping a single authoritative record.

Rejected: keeping the old `session_id → record` + `lawyer → session_id`
indirection as primary. It costs the worker two GETs per lawyer and was already
the slower of the two designs.

### ADR-3 — Store becomes async-native (`redis.asyncio`)

**Decision.** `SessionStore` methods become `async def` backed by
`redis.asyncio`. This fixes the proposal's "sync blocking I/O in async handler"
issue (`store.save_session(...)` is currently a sync call inside the async
`/login/clave-unica` handler and inside the async worker).

Rationale: **every** caller already runs in an async context — FastAPI handlers
and the `AsyncIOScheduler` worker. There is no sync caller to preserve, so the
honest fix is an async store, not `asyncio.to_thread` wrapping.

Method renames: `save_session`→`asave_session`, `get_session_by_lawyer`→
async, `get_all_active_sessions`→async, etc. `get_redis_client` gains an async
variant `get_async_redis_client()` returning `redis.asyncio.Redis`.

Rejected: `asyncio.to_thread(store.save_session, ...)` at call sites. Rejected:
hides blocking I/O behind every call and leaves a sync client that invites future
event-loop stalls.

### Migration / coexistence path (no mid-refactor breakage)

The change lands in 3 PRs; within slice 1 we cannot leave callers half-wired.
Strategy:

1. Introduce `pjud_session.py` + async `SessionStore` in the **same** slice-1 PR
   that rewires every current caller (`auth.py`, `pjud.py`, `sync_scheduler.py`,
   `clave_unica.py`). There are only 4 call sites — small enough to flip atomically.
2. Delete `SessionManager` in the same PR; `base.py` stops taking a
   `session_manager` and returns a bare `PJUDSession`.
3. `from_redis` tolerates a missing `auth_method` and naive timestamps so any
   session already cached in a live Redis is read without a crash (then expires
   naturally within 25 min). No data migration needed — sessions are ephemeral.

## 4. Decision: Login Flow Wiring

### ADR-4 — `login_with_token` is the single captcha entry; `refresh` = re-login

Both captcha endpoints (`/auth/login` and `/pjud/login`) call
`scraper.login_with_token(rut, password, captcha_token)` (the real method at
`base.py:306`). `login_with_user_captcha` and `refresh_session_with_captcha` are
removed; `auth.py` stops importing them.

Captcha login sequence:

```
POST /auth/login
  scraper.start()
  raw = await scraper.login_with_token(rut, password, captcha_token)   # cookies+storage
  lawyer = await _get_or_create_lawyer(db, rut, password, "captcha")    # §5
  session = PJUDSession.create(session_id=uuid4(), lawyer_id=lawyer.id,
              rut=normalize(rut), cookies=raw.cookies,
              local_storage=raw.local_storage, auth_method="captcha")
  await store.asave_session(session)
  return JWT(sub=rut) + LawyerInfo
```

Clave Única login sequence (structurally already closest):

```
POST /auth/login/clave-unica
  lawyer = await _get_or_create_lawyer(db, rut, password, "clave_unica")
  session = await ClaveUnicaAuth.login(page, creds, lawyer.id)   # returns PJUDSession
  await store.asave_session(session)                              # async now (ADR-3)
  return JWT(sub=rut) + session_id + LawyerInfo
```

`refresh` becomes a full re-login. The endpoint has only the RUT (JWT `sub`);
it resolves the lawyer, decrypts the stored PJUD password, and calls
`login_with_token` with the **new** captcha token from the request:

```
POST /auth/refresh
  rut = current_lawyer (JWT)
  lawyer = lawyer_by_rut(db, rut)
  password = decrypt_pjud_password(lawyer.encrypted_pjud_password)
  raw = await scraper.login_with_token(rut, password, request.captcha_token)
  session = PJUDSession.create(...auth_method="captcha")
  await store.asave_session(session)
```

This is why credential storage (slice 3) and refresh are coupled: an unattended
or token-only refresh needs the stored password.

**ADR-4b — Standardize expiry to ~25 min UTC.** The two old models disagreed
(25 min local-time vs 2 h `utcnow`). Canonical: `DEFAULT_SESSION_MINUTES = 25`,
UTC-aware throughout, TTL = `time_until_expiry()`. The 2-hour value was wrong
(contradicts the real PJUD timeout) and is dropped. Consequence: the scheduled
sync interval (4 h) is far longer than session life, which is exactly why
autonomous worker re-auth (slice 3) is mandatory, not optional.

## 5. Decision: Lawyer Resolution (`_get_or_create_lawyer`)

### ADR-5 — Idempotent, RUT-keyed, in the request's DB session

```python
async def _get_or_create_lawyer(
    db: Session, rut: str, password: str, auth_method: str = "captcha",
) -> Lawyer:
    rut_norm = normalize_rut(rut)                  # one shared normalizer
    lawyer = db.query(Lawyer).filter(Lawyer.rut == rut_norm).first()
    if lawyer is None:
        lawyer = Lawyer(rut=rut_norm, name=rut_norm,   # name placeholder = rut
                        preferred_auth_method=auth_method, is_active=True)
        db.add(lawyer)
    if auth_method == "captcha":
        lawyer.encrypted_pjud_password = encrypt_pjud_password(password)
    else:
        lawyer.clave_unica_rut = rut_norm
        lawyer.encrypted_clave_unica_password = encrypt_pjud_password(password)
        lawyer.preferred_auth_method = "clave_unica"
    lawyer.last_login_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(lawyer)
    return lawyer
```

- **Idempotency** rests on the existing `lawyers.rut` UNIQUE index — concurrent
  first-logins race to insert; the loser catches `IntegrityError` and re-queries.
- Endpoints gain `db: Session = Depends(get_db)`; `auth.py` handlers currently
  take no DB session — that dependency is added in slice 2.
- This kills `lawyer_id=0` (`pjud.py:191`) and `lawyer_id=1` (`auth.py` clave
  única default). The session's `lawyer_id` now comes from the resolved row.

Note: credential *writing* lives here, but the proposal slices it as PR 3. To
keep slices clean: **slice 2** lands resolution that writes only identity
(`rut`, `name`, `last_login_at`); **slice 3** extends the same function to
populate the encrypted-credential columns. Same function, additive change.

## 6. Decision: Encrypted Credentials + Autonomous Worker Re-Auth

### ADR-6 — Reuse existing columns; NO new migration

**Discovery (verified in code):** the credential schema **already exists**:
- `lawyers.encrypted_pjud_password` — migration `001_initial_schema.py:29`.
- `lawyers.clave_unica_rut`, `encrypted_clave_unica_password`,
  `preferred_auth_method` — migration `003_add_clave_unica_fields.py`.

Therefore slice 3 needs **no new migration and no schema change** — it is pure
wiring: populate these columns on login (ADR-5) and read+decrypt them in the
worker. This materially shrinks slice 3. Encryption uses the existing reversible
Fernet helpers `encrypt_pjud_password`/`decrypt_pjud_password`
(`app/core/security.py`).

> **SECURITY REVIEW REQUIRED.** Storing PJUD/Clave Única passwords with
> *reversible* Fernet encryption is a deliberate trade-off: the worker must
> replay the password to PJUD to re-authenticate unattended, so hashing is not
> an option. Mitigations to confirm in review: `ENCRYPTION_KEY` sourced from a
> secret manager (not env in repo), key-access restricted to the worker/api
> roles, and key rotation procedure. This must be signed off before slice 3 merges.

### ADR-7 — Worker re-auth strategy on expiry

In `sync_lawyer_cases`, when `get_session_by_lawyer` returns `None`, attempt one
autonomous re-auth before skipping:

```
session = await store.get_session_by_lawyer(lawyer_id)
if session is None:
    session = await _reauth(lawyer, store)        # may return None
    if session is None:
        return {"skipped": True, "reason": reason}  # e.g. captcha_no_2captcha_key
```

`_reauth` branches on `lawyer.preferred_auth_method`:

- **clave_unica (always available):** decrypt `encrypted_clave_unica_password`
  → headless `ClaveUnicaAuth.login` → `store.asave_session`. No human, no captcha.
- **captcha (conditional):** needs a reCAPTCHA token, which requires a paid
  2Captcha solve. Gate on `settings.CAPTCHA_API_KEY`:
  - configured → solve token via 2Captcha client → `login_with_token` → store.
  - empty → return `None` with reason `captcha_no_2captcha_key`; the worker logs
    and skips that lawyer (data stays stale, no crash).

**2Captcha config flag location:** `settings.CAPTCHA_API_KEY` already exists
(default `""`). Add a derived property `settings.has_2captcha → bool(CAPTCHA_API_KEY)`
as the single decision point; do not scatter `if key` checks.

Trade-off called out by the proposal: the captcha path cannot be fully
autonomous without paid 2Captcha, so Clave Única is the only reliably unattended
method. This is a product constraint, documented, not a bug.

## 7. Decision: Test Strategy (mock-based, the definition of "done")

E2E is hard-blocked (reCAPTCHA token, real creds, Chromium, live PJUD). "Done"
is proven by mock tests + mypy. Each external dependency gets a seam:

| Dependency | Seam | Mock approach |
|---|---|---|
| Redis | `SessionStore(redis=...)` injectable client | `fakeredis.aioredis.FakeRedis` — real TTL/round-trip semantics, no server |
| Playwright / browser | `BrowserFactory`, `page` | `AsyncMock` page; patch `BrowserFactory.__aenter__`; `ClaveUnicaAuth.login`/`login_with_token` return a fixture `PJUDSession` |
| PJUD HTTP (scraping) | `scraper.get_my_cases` | patched to return a fixture `list[PJUDCase]` |
| Postgres | `_get_or_create_lawyer(db, ...)` | in-memory SQLite session fixture (`conftest`) |
| 2Captcha | `settings.has_2captcha` + solver client | toggle flag; `MagicMock` solver returns a fake token |

`conftest.py` fixtures: `fake_redis`, `session_store(fake_redis)`, `db_session`
(SQLite, create_all/rollback), `sample_session` (canonical `PJUDSession`),
`mock_scraper`. Async tests use `@pytest.mark.asyncio` (pytest-asyncio).

Tests map 1:1 to success criteria:
1. **Round-trip / unification:** scraper-side `asave_session(s)` then worker-side
   `get_session_by_lawyer(s.lawyer_id)` returns an equal session — the exact bug
   the dual layer caused. Also assert secondary-by-id and by-rut lookups resolve.
2. **Method rename:** `/auth/login` (TestClient + mocked scraper) succeeds and
   asserts `login_with_token` was awaited; assert no `login_with_user_captcha`
   symbol remains (import-time / `hasattr` guard).
3. **No hardcodes:** after login, stored session `lawyer_id` equals the resolved
   `Lawyer.id` (not 0/1); `_get_or_create_lawyer` is idempotent across two calls.
4. **Autonomous re-auth:** with an expired/empty session, worker re-auths via
   mocked Clave Única and proceeds to sync; with `auth_method="captcha"` and
   `has_2captcha=False`, worker skips with `captcha_no_2captcha_key` and does not
   crash.
5. **mypy clean** on touched modules (gated in CI / verify phase).

## 8. Side-Bug Fixes (folded into the relevant slices)

| Bug | Location | Fix | Slice |
|---|---|---|---|
| `import asyncio` at file bottom | `civil.py:617` | move to the top import block with the other stdlib imports | 1 (mechanical, low risk) |
| naive vs UTC expiry mismatch | `session_manager.is_expired` (`datetime.now()` local) vs `session_store` (`utcnow`) | canonical model is UTC-aware throughout (ADR-4b); aware-vs-aware compare | 1 |
| failed sync leaves `cases_found` unset | `sync_scheduler.sync_lawyer_cases` except-block | on the failed `SyncHistory`, set `cases_found=0` (and complete fields) so the record is consistent | 3 (touches worker anyway) |

## 9. Slice Boundaries (aligns with proposal's 3 chained PRs)

- **Slice 1 — unify session + method rename.** New `pjud_session.py`; async
  `SessionStore` (ADR-2/3); delete `SessionManager`; rewire all 4 call sites;
  `login_with_token` everywhere; `refresh` re-login skeleton; UTC consistency;
  `civil.py` import fix. `lawyer_id` still threaded (resolution lands next).
  Proven by round-trip + rename mock tests.
- **Slice 2 — lawyer resolution.** Real `_get_or_create_lawyer` (RUT-keyed,
  idempotent, identity-only writes); add `db` dependency to handlers; kill
  `lawyer_id=0/1`. Sessions bind to real `lawyer_id`. Proven by no-hardcode +
  idempotency tests.
- **Slice 3 — encrypted creds + worker re-auth.** Extend `_get_or_create_lawyer`
  to populate encrypted columns (no migration — ADR-6); worker `_reauth`
  (Clave Única always, captcha conditional on `has_2captcha`); `sync_scheduler`
  `cases_found` fix; **security sign-off gate**. Proven by autonomous-re-auth +
  captcha-skip tests.

## 10. Risks / Open Items

- **R1 (security gate):** reversible credential storage — must clear review
  before slice 3 merges (ADR-6).
- **R2 (blast radius):** async store conversion touches 4 call sites + worker;
  mitigated by landing them atomically in slice 1 with mock coverage + mypy.
- **R3 (JWT carries RUT, not lawyer_id):** status/logout resolve via the
  `rut → lawyer_id` secondary index. A future cleanup could add a `lawyer_id`
  JWT claim; out of scope here to limit blast radius. Assumption flagged.
- **R4 (RUT normalization):** captcha login strips the verification digit
  (`base.py`), Clave Única may use a different RUT. The store's RUT index and
  `_get_or_create_lawyer` must use **one** shared `normalize_rut` helper or the
  by-RUT lookups and lawyer rows will diverge. Single normalizer is mandatory.
- **R5 (2Captcha cost):** autonomous captcha sync incurs per-solve cost;
  defaulted off via empty `CAPTCHA_API_KEY`.
