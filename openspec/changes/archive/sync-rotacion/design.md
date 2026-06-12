# Design: Sync Detail Rotation (Block 2)

## Technical Approach

Approach A (round-robin). DB state is the source of truth for which cases get a
detail scrape each scheduled run. A new nullable `cases.last_detail_checked_at`
column drives an `ORDER BY last_detail_checked_at ASC NULLS FIRST, filed_at DESC
LIMIT batch` selection. The scheduled path swaps the front-of-list `[:5]` cap for
this rotation batch; the on-demand ROL-targeted path is untouched. The timestamp
advances after every successful `get_case_detail` (even 0-movement fetches),
guaranteeing fairness and no starvation. Slice 2 adds a dependency-injected
re-auth callback so the service recovers from mid-batch session expiry without
coupling to the scheduler's auth logic.

## Architecture Decisions

| Decision | Choice | Rejected | Rationale |
|---|---|---|---|
| Selection source | DB query ordered by `last_detail_checked_at` | Slice `api_cases[:n]` (PJUD order) | PJUD list order varies per run and has no change signal; DB state gives deterministic fairness |
| New fn vs. extend old | New `_select_cases_for_detail_rotation`; keep `_select_cases_for_movement_check` for ROL path | Overload one fn | On-demand ROL targeting and scheduled rotation are different concerns; separation keeps both testable |
| Pass batch into service | Add `selected_cases: Optional[list]=None` to `detect_and_sync_movements`; when set, skip internal selection | Move DB query into the service | Service stays free of competencia/rotation knowledge; scheduler owns selection. ROL/on-demand callers keep passing `api_cases`+`rol` |
| Index | Single btree `ix_cases_last_detail_checked_at` | Composite `(lawyer_id, competencia, last_detail_checked_at)`; no index | Helps the ORDER BY at ~2.5k rows/lawyer; single-column is reversible and low-cost. Composite noted as future tuning |
| NULLS FIRST portability | SQLAlchemy `.asc().nullsfirst()` | Raw SQL | Works on Postgres and SQLite 3.30+ (tests). SQLite ASC already nulls-first by default |
| Re-auth coupling | Inject `reauth_callback` into service | Import `_reauth` in sync_service | Keeps sync_service decoupled from scheduler/Playwright auth; callback built by scheduler |
| Expiry exception | Catch `SessionExpiredError` before the generic `except Exception` | Rely on existing `except Exception` | Generic handler currently swallows expiry as a per-case error; a dedicated clause is required to trigger reauth + retry |

## Data Flow

    sync_lawyer_cases (scheduler, has competencia)
       │  get_my_cases → sync_cases (upsert list, populates DB rows)
       │  _select_cases_for_detail_rotation(db, lawyer_id, competencia, api_cases, DETAIL_BATCH_SIZE)
       │     └─ DB: ORDER BY last_detail_checked_at ASC NULLS FIRST, filed_at DESC LIMIT N
       │        → join rols to api_cases for live case_token (skip if absent/no token)
       ▼
    detect_and_sync_movements(selected_cases=batch, delay=DETAIL_FETCH_DELAY, reauth_callback)
       │  per case: get_case_detail → movements/entities/docs
       │  set db_case.last_detail_checked_at = utcnow() → db.commit()
       └─ SessionExpiredError → reauth_callback() → swap session → retry once → else stop batch

## File Changes

| File | Action | Description |
|---|---|---|
| `alembic/versions/007_add_last_detail_checked_at.py` | Create | Add nullable column + index; reversible downgrade drops both |
| `app/models/case.py` | Modify | `last_detail_checked_at = Column(DateTime, nullable=True, index=True)` |
| `app/config.py` | Modify | `DETAIL_BATCH_SIZE: int = 30`, `DETAIL_FETCH_DELAY: float = 2.0` |
| `app/services/sync_service.py` | Modify | New `_select_cases_for_detail_rotation`; `selected_cases` + `reauth_callback` params; set timestamp; expiry retry |
| `app/workers/sync_scheduler.py` | Modify | Build rotation batch, pass `selected_cases` + `DETAIL_FETCH_DELAY`; Slice 2: build `reauth_callback` wrapping `_reauth(lawyer, store)` |

## Interfaces / Contracts

```python
def _select_cases_for_detail_rotation(
    db: Session, lawyer_id: int, competencia: str,
    api_cases: list, batch_size: int,
) -> list:  # PJUDCase objects present in api_cases with a valid case_token
    # Query DB rols ordered by last_detail_checked_at ASC NULLS FIRST, filed_at DESC,
    # LIMIT batch_size; map each to api_cases by normalized rol; drop misses/no-token.
    # Empty DB result → fallback to api_cases[:batch_size].

async def detect_and_sync_movements(
    ..., selected_cases: Optional[list] = None,
    reauth_callback: Optional[Callable[[], Awaitable[Optional["PJUDSession"]]]] = None,
) -> Tuple[int, int, List[str]]:
    # selected_cases set → use directly (bypass _select_cases_for_movement_check).
    # Mark-checked: inside the db_case-found branch, set
    #   db_case.last_detail_checked_at = datetime.utcnow() immediately before db.commit().
```

Slice 2 retry: extract per-case body into an inner coroutine. Loop:
`except (SessionExpiredError, SessionNotAuthenticatedError):` if callback → `new = await callback()`; if `new`,
reassign local `pjud_session`, retry the case once; on second expiry, log and
`break` (graceful partial batch). Generic `except Exception` stays for other
errors. Both exception types imported from `app.scrapper.pjud.exceptions`.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | Rotation order + batch limit | Seed Case rows with mixed `last_detail_checked_at` incl. NULL; assert NULLS-FIRST then `filed_at DESC`, capped at batch_size (SQLite session) |
| Unit | Token join / skip | DB rol absent from `api_cases` or no token → excluded |
| Unit | Empty-DB fallback | No rows → `api_cases[:batch_size]` |
| Integration | Mark-checked advances | After a fetch, `last_detail_checked_at` is set even on 0 movements; committed |
| Integration | No starvation | Repeated runs eventually cover all rows (oldest always next) |
| Integration | Migrate old assertion | The test asserting `await_count == MOVEMENT_CHECK_DEFAULT_MAX` becomes a rotation-fn test over DB rows |
| Integration (S2) | Reauth + retry | First `get_case_detail` raises `SessionExpiredError` → callback invoked → retry succeeds; second expiry → batch stops gracefully |

Mock scraper/store; SQLite for DB; `pytest.mark.asyncio` for async paths.

## Migration / Rollout

Additive nullable column + index — `alembic upgrade head` is safe online, no
backfill (NULL means never-checked → highest rotation priority). `downgrade -1`
drops index then column. Sliceable: Slice 1 = migration, column, config,
rotation fn, mark-checked, wiring, tests (rotation behavior). Slice 2 =
`reauth_callback`, expiry retry, coverage logging (`detail checked X/Y, oldest=Z`).

## Open Questions

- [x] Runtime-observable, not a blocker: is the ~2524 case count per-lawyer or
  total across lawyers? Resolve via the Slice 2 coverage log; tune
  `DETAIL_BATCH_SIZE` after observing real cycle time and PJUD rate behavior.
  (Accepted as runtime-observable; coverage log now available via _oldest_unchecked_label.)
