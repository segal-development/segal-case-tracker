# Tasks: sync-rotacion

**Change:** sync-rotacion  
**TDD Mode:** STRICT — `.venv/bin/python -m pytest`  
**Verify gate:** `pytest -m "not integration"` + `mypy app/core` + alembic up/down for the migration  
**Chained PRs:** Yes — Slice 1 and Slice 2 are separate PRs (see Review Workload Forecast)  
**Chain strategy:** stacked-to-main (Slice 1 → main first; Slice 2 branches from Slice 1)

---

## Dependency Graph

```
S1-T1 ──────────────────────────────────────────────────┐
S1-T2 ──── [parallel to S1-T1] ────────────────────────►├─► S1-T3 ─► S1-T4 ─► S1-T5 ─► S1-T6 ─► S1-T7 ─► S1-V1
                                                          │
                                                          └─────── (S1-T1 and S1-T2 both needed by S1-T3)

S1-V1 ─► S2-T1 ─► S2-T2 ─► S2-T3 ─[parallel]─► S2-T4 ─► S2-V1
                                  └──────────────► S2-T4 ─┘
```

---

## SLICE 1 — Migration, Rotation Selection, Mark-Checked, Config, Wiring

> PR target: `main`  
> Rollback boundary: `alembic downgrade -1` drops index + column cleanly; scheduler falls back to MOVEMENT_CHECK_DEFAULT_MAX path if PR is reverted.

---

### S1-T1 [TEST] Rotation-selection unit tests — new file

**File:** `tests/services/test_detail_rotation.py` (create)  
**Spec:** Rotation-Aware Case Selection · Empty DB Graceful Fallback · Configurable Batch Size  
**Parallel with:** S1-T2

Write a pytest class `TestSelectCasesForDetailRotation` with the following test methods.
Each test seeds a SQLite in-process DB session (reuse the existing `db` fixture from conftest).
All tests FAIL until S1-T3 is implemented.

```
test_nulls_come_before_checked_cases
  GIVEN 3 cases: two with last_detail_checked_at=NULL, one with a past timestamp
  WHEN _select_cases_for_detail_rotation(db, lawyer_id, competencia, api_cases, batch_size=10)
  THEN the two NULL cases appear before the checked case in the returned list

test_secondary_sort_filed_at_desc_within_null_group
  GIVEN 2 cases both with last_detail_checked_at=NULL but different filed_at dates
  WHEN selection runs
  THEN the case with the MORE RECENT filed_at is returned first

test_batch_capped_at_batch_size
  GIVEN 7 DB cases, api_cases for all 7, batch_size=3
  WHEN selection runs
  THEN exactly 3 cases are returned

test_token_join_excludes_absent_rol
  GIVEN a DB case whose rol is not present in api_cases
  WHEN selection runs
  THEN that case is NOT in the returned list

test_no_token_excludes_case
  GIVEN a DB case whose rol matches an api_case where api_case.case_token is None
  WHEN selection runs
  THEN that case is NOT in the returned list

test_empty_db_fallback_returns_api_cases_slice
  GIVEN no Case rows in DB for this lawyer + competencia
  AND api_cases has 10 entries
  AND batch_size=4
  WHEN selection runs
  THEN exactly 4 api_cases are returned AND no exception propagates
```

Helpers needed in the file:
- `_seed_case(db, lawyer_id, competencia, rol, filed_at, last_detail_checked_at)` — creates Lawyer+Court+Case rows; returns the Case.
- `_make_api_case(rol, case_token)` — minimal MagicMock with `rol` and `case_token`.

Marker: no extra marker needed (these are pure unit tests with SQLite).

---

### S1-T2 [IMPL] Migration 007 + ORM column + config knobs

**Files:**
- `alembic/versions/007_add_last_detail_checked_at.py` (create)
- `app/models/case.py` (modify)
- `app/config.py` (modify)

**Spec:** Rotation-Aware Case Selection (column) · Configurable Batch Size and Fetch Delay (config)  
**Parallel with:** S1-T1  
**Work-unit commit:** these three changes ship in one commit (`feat(rotation): migration 007, ORM column, config knobs`)

Migration `007_add_last_detail_checked_at.py`:
- `revision = "007"`, `down_revision = "006"`
- Upgrade: `op.add_column("cases", Column("last_detail_checked_at", DateTime, nullable=True))` then `op.create_index("ix_cases_last_detail_checked_at", "cases", ["last_detail_checked_at"])`
- Downgrade: `op.drop_index("ix_cases_last_detail_checked_at", "cases")` then `op.drop_column("cases", "last_detail_checked_at")`

`app/models/case.py`:
- Add `last_detail_checked_at = Column(DateTime, nullable=True)` (after `last_movement_at`)

`app/config.py` (near `DOC_DOWNLOAD_ENABLED`, ~line 88):
- Add `DETAIL_BATCH_SIZE: int = 30`
- Add `DETAIL_FETCH_DELAY: float = 2.0`

Verify inline: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` — no errors.

---

### S1-T3 [IMPL] `_select_cases_for_detail_rotation` function

**File:** `app/services/sync_service.py` (modify — add new function before `detect_and_sync_movements`)  
**Spec:** Rotation-Aware Case Selection · Empty DB Graceful Fallback  
**Depends on:** S1-T1 (tests written), S1-T2 (ORM column exists)  
**Makes S1-T1 tests pass.**

Add `_select_cases_for_detail_rotation` with this contract:

```python
def _select_cases_for_detail_rotation(
    db: Session,
    lawyer_id: int,
    competencia: str,
    api_cases: list,
    batch_size: int,
) -> list:
```

Implementation steps:
1. Build a lookup dict from api_cases: `{normalize(ac.rol): ac for ac in api_cases if ac.case_token}`.
2. Query DB: `db.query(Case).filter(Case.lawyer_id == lawyer_id, Case.competencia == competencia).order_by(Case.last_detail_checked_at.asc().nullsfirst(), Case.filed_at.desc()).limit(batch_size).all()`
3. For each DB row, look up its normalized rol in the api_cases dict. Skip if absent or no token.
4. Collect the matching api_case objects preserving the DB order.
5. If the DB query returns 0 rows (empty DB for this lawyer+competencia): return `api_cases[:batch_size]`.
6. Return the collected list (may be shorter than batch_size if some rols had no match/no token).

Import: `Case.last_detail_checked_at.asc().nullsfirst()` requires `from sqlalchemy import asc, nullsfirst` or use the column method directly.

---

### S1-T4 [TEST] Mark-checked behavior tests + case-error advancement test

**File:** `tests/services/test_detail_rotation.py` (extend) OR a new class in `tests/workers/test_movement_detection.py`  
**Spec:** Mark Checked After Successful Detail Fetch · Full-Cycle Coverage (No Starvation) · robustness requirement  
**Depends on:** S1-T3 (function importable; tests call `detect_and_sync_movements`)

Write a class `TestMarkDetailCheckedAt` with:

```
test_timestamp_advances_on_zero_movement_success
  GIVEN a Case in DB, api_cases contains it, get_case_detail returns 0 movements
  WHEN detect_and_sync_movements(db, ..., api_cases=[case], selected_cases=[api_case])
  THEN db_case.last_detail_checked_at is NOT None after the call
  AND it is close to utcnow()

test_timestamp_advances_on_case_specific_error
  GIVEN a Case in DB, api_cases contains it
  AND get_case_detail raises a generic Exception (not SessionExpiredError)
  WHEN detect_and_sync_movements runs
  THEN db_case.last_detail_checked_at IS advanced (case rotates to back)
  AND the error is captured in the returned errors list (function does not raise)

test_timestamp_not_set_when_db_case_not_found
  GIVEN api_cases contains a case whose rol does NOT exist in the DB
  AND get_case_detail succeeds
  WHEN detect_and_sync_movements runs
  THEN no row is updated (no crash, just a logged warning)

test_case_specific_error_does_not_starve_next_run
  GIVEN two DB cases; the first raises a generic Exception, the second succeeds
  WHEN detect_and_sync_movements runs with selected_cases=[both]
  THEN both cases have non-null last_detail_checked_at
  (first advanced in except branch; second advanced in success branch)
```

All tests fail until S1-T5 is implemented.  
Use `@pytest.mark.asyncio` for async tests.

---

### S1-T5 [IMPL] `detect_and_sync_movements` changes

**File:** `app/services/sync_service.py` (modify)  
**Spec:** Mark Checked After Successful Detail Fetch · Full-Cycle Coverage · robustness requirement  
**Depends on:** S1-T4 (tests written), S1-T3 (function exists)  
**Makes S1-T4 tests pass.**

Changes to `detect_and_sync_movements`:

1. Add parameter: `selected_cases: Optional[list] = None` (before `delay_between_fetches`)

2. Replace:
   ```python
   cases_for_check = _select_cases_for_movement_check(api_cases, rol=rol)
   ```
   With:
   ```python
   if selected_cases is not None:
       cases_for_check = selected_cases
   else:
       cases_for_check = _select_cases_for_movement_check(api_cases, rol=rol)
   ```

3. Move the DB case lookup BEFORE the per-case try block (so the except branch can reference it):
   ```python
   normalized_rol = api_case.rol.strip().upper()
   db_case = db.query(Case).filter(
       Case.lawyer_id == lawyer_id,
       Case.rol == normalized_rol,
   ).first()
   try:
       detail = await scraper.get_case_detail(...)
       ...
   ```
   Remove the duplicate lookup that currently exists inside the try block.

4. In the success path, immediately before `db.commit()` (~line 1239), add:
   ```python
   if db_case is not None:
       db_case.last_detail_checked_at = datetime.utcnow()
   ```

5. In the `except Exception` handler, BEFORE `logger.error`, add:
   ```python
   if db_case is not None:
       db_case.last_detail_checked_at = datetime.utcnow()
       try:
           db.commit()
       except Exception:
           db.rollback()
   ```
   This ensures a persistently-failing case rotates to the back. Rollback guard prevents a commit failure from masking the original error.

Import guard: ensure `datetime` is imported from the stdlib (`from datetime import datetime`).

---

### S1-T6 [IMPL] Scheduler wiring

**File:** `app/workers/sync_scheduler.py` (modify)  
**Spec:** Configurable Batch Size and Fetch Delay · Isolation from Case-List Refresh  
**Depends on:** S1-T5 (new params available)

At `sync_lawyer_cases`, after `sync_cases` (upsert list) completes:

1. Import at top of function (or at module top): `from app.services.sync_service import _select_cases_for_detail_rotation`

2. Add rotation batch selection:
   ```python
   rotation_batch = _select_cases_for_detail_rotation(
       db=db,
       lawyer_id=lawyer_id,
       competencia=competencia,
       api_cases=cases,
       batch_size=settings.DETAIL_BATCH_SIZE,
   )
   ```

3. Replace the `detect_and_sync_movements` call (~line 281-288):
   ```python
   movements_new, alerts_created, mov_errors = await detect_and_sync_movements(
       db=db,
       scraper=scraper,
       pjud_session=session,
       lawyer_id=lawyer_id,
       api_cases=cases,
       selected_cases=rotation_batch,
       delay_between_fetches=settings.DETAIL_FETCH_DELAY,
   )
   ```
   The hardcoded `delay_between_fetches=1.0` is replaced by `settings.DETAIL_FETCH_DELAY`.

---

### S1-T7 [TEST] Migrate `test_selects_cases_using_movement_check_scoping`

**File:** `tests/workers/test_movement_detection.py` (modify)  
**Spec:** Rotation-Aware Case Selection · ROL-Targeted On-Demand Path Unchanged  
**Depends on:** S1-T5 (new `selected_cases` param available)

The existing `test_selects_cases_using_movement_check_scoping` asserts that `detect_and_sync_movements` without `selected_cases` caps at `MOVEMENT_CHECK_DEFAULT_MAX` calls — this still tests the on-demand path and remains valid. Keep it.

ADD a new test method to `TestDetectAndSyncMovements`:

```
test_selected_cases_bypasses_movement_check_scoping
  GIVEN api_cases has MOVEMENT_CHECK_DEFAULT_MAX + 3 entries
  AND selected_cases is a list of exactly 2 pre-selected api_cases
  WHEN detect_and_sync_movements(db, ..., selected_cases=selected_cases)
  THEN get_case_detail.await_count == 2
  (not MOVEMENT_CHECK_DEFAULT_MAX — the bypass is exercised)
```

This validates that `selected_cases` overrides the internal cap, which is the core scheduler integration point.

---

### S1-V1 [VERIFY] Slice 1 verification gate

**Depends on:** S1-T7 (all Slice 1 impl + test tasks done)

Run in order:

```bash
# 1. Migration round-trip
alembic upgrade head
alembic downgrade -1
alembic upgrade head

# 2. Unit + non-integration tests (no regressions, new rotation tests pass)
.venv/bin/python -m pytest -m "not integration and not slice2" -v \
  tests/services/test_detail_rotation.py \
  tests/workers/test_movement_detection.py

# 3. Full non-integration suite (detect regressions)
.venv/bin/python -m pytest -m "not integration" --tb=short

# 4. Type check
mypy app/core
```

All must pass. Zero new failures in the full suite.

Rollback: `git revert` the Slice 1 commits; `alembic downgrade -1` — no data loss (column is nullable, NULL = never-checked).

---

## SLICE 2 — Re-Auth Callback, Session-Expiry Retry, Observability

> PR target: `main` (after Slice 1 is merged)  
> Rollback boundary: reverts to Slice 1 state; no schema change; scheduler simply omits `reauth_callback`.

---

### S2-T1 [TEST] Slice 2 re-auth tests

**File:** `tests/services/test_detail_rotation.py` (extend with new class `TestReauthMidBatch`)  
**Spec:** Mid-Batch Session Re-Auth [SLICE 2]  
**Depends on:** S1-V1 green

Mark all tests in this class with `@pytest.mark.slice2` AND `@pytest.mark.asyncio`.
All tests FAIL until S2-T2 is implemented.

```
test_reauth_succeeds_case_is_retried
  GIVEN get_case_detail raises SessionExpiredError on first call, succeeds on second
  AND reauth_callback returns a valid PJUDSession object
  WHEN detect_and_sync_movements(db, ..., selected_cases=[api_case], reauth_callback=callback)
  THEN get_case_detail.await_count == 2 (first attempt + retry)
  AND last_detail_checked_at IS advanced (retry succeeded)
  AND no exception propagates

test_reauth_fails_batch_stops_gracefully
  GIVEN get_case_detail raises SessionExpiredError
  AND reauth_callback also raises an exception
  WHEN detect_and_sync_movements runs with selected_cases=[case_a, case_b]
  THEN only case_a was attempted (case_b skipped)
  AND last_detail_checked_at NOT advanced for case_a (session error, not case fault)
  AND no unhandled exception propagates
  AND returned errors list contains an entry for case_a

test_reauth_returns_none_batch_stops_gracefully
  GIVEN get_case_detail raises SessionExpiredError
  AND reauth_callback returns None
  WHEN detect_and_sync_movements runs with selected_cases=[case_a, case_b]
  THEN batch stops after case_a; case_b not processed
  AND no unhandled exception propagates

test_session_error_does_not_advance_timestamp
  GIVEN get_case_detail raises SessionExpiredError on case_a
  AND reauth_callback raises (re-auth fails)
  WHEN detect_and_sync_movements runs
  THEN case_a.last_detail_checked_at is NOT advanced
  (session failure is not the case's fault — rotation position preserved)
```

---

### S2-T2 [IMPL] `reauth_callback` + `SessionExpiredError` handling

**File:** `app/services/sync_service.py` (modify)  
**Spec:** Mid-Batch Session Re-Auth [SLICE 2]  
**Depends on:** S2-T1 (tests written)  
**Makes S2-T1 tests pass.**

Changes to `detect_and_sync_movements`:

1. Add parameter (after `selected_cases`):
   ```python
   reauth_callback: Optional[Callable[[], Awaitable[Optional["PJUDSession"]]]] = None,
   ```

2. Add import: `from app.scrapper.pjud.exceptions import SessionExpiredError`

3. Extract per-case detail fetch into an inner coroutine `_do_fetch()` that encapsulates lines from `detail = await scraper.get_case_detail(...)` through the entity sync and document download.  This makes retry-once straightforward.

4. In the per-case loop, restructure exception handling:
   ```python
   try:
       await _do_fetch()
       if db_case is not None:
           db_case.last_detail_checked_at = datetime.utcnow()
           # (already done before db.commit() in success path)
   except SessionExpiredError:
       # Do NOT advance last_detail_checked_at — not the case's fault
       new_session = None
       if reauth_callback is not None:
           try:
               new_session = await reauth_callback()
           except Exception as reauth_exc:
               logger.error("detect_and_sync_movements: reauth failed: %s", reauth_exc)
       if new_session is None:
           logger.warning("detect_and_sync_movements: stopping batch — session expired, no reauth")
           errors.append(f"Session expired processing {api_case.rol}; batch stopped")
           break
       pjud_session = new_session  # reassign local for remaining cases
       # Retry once
       try:
           await _do_fetch()
           if db_case is not None:
               db_case.last_detail_checked_at = datetime.utcnow()
       except SessionExpiredError:
           logger.error("detect_and_sync_movements: second expiry after reauth; stopping batch")
           errors.append(f"Session expired again on retry for {api_case.rol}; batch stopped")
           break
       except Exception as retry_exc:
           if db_case is not None:
               db_case.last_detail_checked_at = datetime.utcnow()
               try:
                   db.commit()
               except Exception:
                   db.rollback()
           logger.error("detect_and_sync_movements: retry failed for %s: %s", api_case.rol, retry_exc)
           errors.append(f"Movement fetch failed on retry for {api_case.rol}: {retry_exc}")
   except Exception as exc:
       # Case-specific error — advance timestamp so case rotates to back
       if db_case is not None:
           db_case.last_detail_checked_at = datetime.utcnow()
           try:
               db.commit()
           except Exception:
               db.rollback()
       logger.error("detect_and_sync_movements: failed for %s: %s", api_case.rol, exc)
       errors.append(f"Movement fetch failed for {api_case.rol}: {str(exc)}")
   ```
   The `except SessionExpiredError` block MUST appear BEFORE `except Exception`. Verify this order carefully.

5. Update the function docstring: document `reauth_callback` and `selected_cases` params; document the no-advance guarantee on `SessionExpiredError`.

---

### S2-T3 [IMPL] Scheduler wiring for `reauth_callback`

**File:** `app/workers/sync_scheduler.py` (modify)  
**Spec:** Mid-Batch Session Re-Auth [SLICE 2]  
**Depends on:** S2-T2  
**Parallel with:** S2-T4

In `sync_lawyer_cases`, build and pass the reauth callback:
```python
async def _reauth_for_lawyer() -> Optional[PJUDSession]:
    return await _reauth(lawyer, store)  # existing _reauth helper

movements_new, alerts_created, mov_errors = await detect_and_sync_movements(
    db=db,
    scraper=scraper,
    pjud_session=session,
    lawyer_id=lawyer_id,
    api_cases=cases,
    selected_cases=rotation_batch,
    delay_between_fetches=settings.DETAIL_FETCH_DELAY,
    reauth_callback=_reauth_for_lawyer,
)
```
Use a local `async def` closure (not a lambda) since the callback is async.

---

### S2-T4 [IMPL] Progress logging (observability)

**File:** `app/services/sync_service.py` (modify, inside `detect_and_sync_movements`)  
**Spec:** Slice 2 progress visibility (design: "coverage log: detail checked X/Y, oldest=Z")  
**Depends on:** S2-T2  
**Parallel with:** S2-T3

After selecting `cases_for_check` (whether from `selected_cases` or `_select_cases_for_movement_check`), log:

```python
logger.info(
    "detect_and_sync_movements: batch=%d / total_api=%d; oldest_unchecked=%s",
    len(cases_for_check),
    len(api_cases),
    _oldest_unchecked_label(db, lawyer_id),
)
```

Add a private helper:
```python
def _oldest_unchecked_label(db: Session, lawyer_id: int) -> str:
    """Return a human-readable age string for the oldest unchecked case, or 'none'."""
    from datetime import timezone
    oldest = (
        db.query(func.min(Case.last_detail_checked_at))
        .filter(Case.lawyer_id == lawyer_id)
        .scalar()
    )
    if oldest is None:
        return "never-checked cases exist"
    age = datetime.utcnow() - oldest
    return f"{age.days}d {age.seconds // 3600}h ago"
```

At the end of the batch loop, log a completion summary:
```python
logger.info(
    "detect_and_sync_movements: done — %d new movements, %d alerts, %d errors",
    movements_new, alerts_created, len(errors),
)
```

Import guard: `from sqlalchemy import func` (if not already imported).

---

### S2-V1 [VERIFY] Slice 2 verification gate

**Depends on:** S2-T4 (all Slice 2 impl + test tasks done)

```bash
# 1. Slice 2 tests pass (new re-auth tests)
.venv/bin/python -m pytest -m "slice2" -v

# 2. Full non-integration suite (including slice2, no regressions)
.venv/bin/python -m pytest -m "not integration" --tb=short

# 3. Type check
mypy app/core

# 4. Manual code review: confirm except SessionExpiredError appears BEFORE except Exception
#    in detect_and_sync_movements — grep for the ordering
```

All must pass.

---

## Review Workload Forecast

| Metric                        | Slice 1    | Slice 2    | Total       |
|-------------------------------|-----------|-----------|-------------|
| Est. production lines changed | ~135      | ~90       | ~225        |
| Est. test lines added         | ~205      | ~90       | ~295        |
| Est. total changed lines      | ~340      | ~180      | ~520        |
| Chained PRs recommended       | Yes       | Yes       | —           |
| 400-line budget risk          | High      | Low       | High total  |
| Decision needed before apply  | No        | No        | —           |

**Notes:**
- Slice 1 alone (~340 lines) is near the 400-line limit. It is intentionally kept as one PR because the migration, ORM, function, and scheduler wiring form a single cohesive behavior unit. A `size:exception` is NOT needed — the estimate is 340 which is under 400. Monitor actual diff before PR creation.
- Slice 2 (~180 lines) is well within budget.
- If Slice 1 actual diff exceeds 400 lines, split out the migration+ORM+config as a micro-PR (#0) and rotate S1-T3 through S1-T7 into Slice 1 proper.

---

## Task Summary Table

| ID     | Type  | File(s)                                    | Spec Req                              | Depends on       | Parallel? |
|--------|-------|--------------------------------------------|---------------------------------------|------------------|-----------|
| S1-T1  | TEST  | tests/services/test_detail_rotation.py     | Rotation-Aware Selection, Empty DB    | —                | with S1-T2|
| S1-T2  | IMPL  | alembic/007, models/case.py, config.py     | Column, Config knobs                  | —                | with S1-T1|
| S1-T3  | IMPL  | app/services/sync_service.py               | Rotation-Aware Selection, Empty DB    | S1-T1, S1-T2     | No        |
| S1-T4  | TEST  | tests/services/test_detail_rotation.py     | Mark Checked, No Starvation, Robustness| S1-T3           | No        |
| S1-T5  | IMPL  | app/services/sync_service.py               | Mark Checked, No Starvation, Robustness| S1-T4           | No        |
| S1-T6  | IMPL  | app/workers/sync_scheduler.py              | Config knobs, Isolation from List     | S1-T5            | No        |
| S1-T7  | TEST  | tests/workers/test_movement_detection.py   | ROL On-Demand Unchanged, selected_cases| S1-T5           | No        |
| S1-V1  | VERIFY| —                                          | All Slice 1                           | S1-T7            | No        |
| S2-T1  | TEST  | tests/services/test_detail_rotation.py     | Mid-Batch Re-Auth [SLICE 2]           | S1-V1            | No        |
| S2-T2  | IMPL  | app/services/sync_service.py               | Mid-Batch Re-Auth [SLICE 2]           | S2-T1            | No        |
| S2-T3  | IMPL  | app/workers/sync_scheduler.py              | Mid-Batch Re-Auth [SLICE 2]           | S2-T2            | with S2-T4|
| S2-T4  | IMPL  | app/services/sync_service.py               | Progress observability                | S2-T2            | with S2-T3|
| S2-V1  | VERIFY| —                                          | All Slice 2                           | S2-T3, S2-T4     | No        |

**Total tasks: 13** (7 Slice 1 + 1 verify + 4 Slice 2 + 1 verify)  
**Parallel pairs: 2** (S1-T1//S1-T2; S2-T3//S2-T4)
