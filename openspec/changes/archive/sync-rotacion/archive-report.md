# Archive Report — sync-rotacion

**Change:** sync-rotacion  
**Archived:** 2026-06-12  
**Branch:** feat/sync-rotacion (tracker — both slices integrated)  
**Artifact store:** Hybrid (Engram + OpenSpec)  
**Verify verdict:** PASS WITH WARNINGS — 0 CRITICAL, 1 WARNING (resolved at archive), 3 SUGGESTIONS  
**SDD cycle:** COMPLETE

---

## Engram Observation IDs (Traceability)

| Artifact | Observation ID |
|----------|---------------|
| proposal | #544 |
| design | #545 |
| spec | #546 |
| tasks | #547 |
| verify-report | #549 |
| archive-report | (saved after this report) |

---

## What Was Shipped

Two stacked PRs, both merged to `main` via the `feat/sync-rotacion` tracker branch.

### Slice 1 — Rotation Foundation

- **Migration 007**: nullable `cases.last_detail_checked_at` column + `ix_cases_last_detail_checked_at` btree index. Additive, fully reversible with `alembic downgrade -1`.
- **ORM column**: `Case.last_detail_checked_at` in `app/models/case.py`.
- **Config knobs**: `DETAIL_BATCH_SIZE: int = 30`, `DETAIL_FETCH_DELAY: float = 2.0` in `app/config.py`. Both env-tunable via Pydantic BaseSettings.
- **`_select_cases_for_detail_rotation`**: new function in `app/services/sync_service.py`. Queries DB ordered by `last_detail_checked_at ASC NULLS FIRST, filed_at DESC`, capped at `DETAIL_BATCH_SIZE`. Joins DB rows to live `api_cases` by normalized ROL; drops misses and no-token entries. Empty-DB fallback: returns `api_cases[:batch_size]`.
- **Timestamp mark-checked**: `detect_and_sync_movements` sets `db_case.last_detail_checked_at = datetime.utcnow()` in the success path (before `db.commit()`). Also advances timestamp in the `except Exception` branch (case-specific errors) so persistently-failing cases rotate to the back. See robustness rule below.
- **Scheduler wiring**: `sync_scheduler.py` calls `_select_cases_for_detail_rotation` after `sync_cases`, passes `selected_cases=rotation_batch` and `delay_between_fetches=settings.DETAIL_FETCH_DELAY` (replaces hardcoded 1.0 s).

### Slice 2 — Mid-Batch Re-Auth + Observability

- **`reauth_callback` parameter**: injected into `detect_and_sync_movements`. Scheduler builds `_reauth_for_lawyer` async closure wrapping `_reauth(lawyer, store)`.
- **Session error handling**: `except (SessionExpiredError, SessionNotAuthenticatedError)` clause BEFORE `except Exception` at sync_service.py:1434. On session error: invoke callback → reassign `pjud_session` → retry case once. Second expiry or callback failure → `break` (batch stops gracefully, no unhandled exception).
- **`SessionNotAuthenticatedError`**: handled symmetrically with `SessionExpiredError`. Extends the Slice 2 spec which mentioned only `SessionExpiredError`; safe superset.
- **Progress logging**: `_oldest_unchecked_label(db, lawyer_id)` helper. Logs `batch=X / total_api=Y; oldest_unchecked=Z` after selection, and a completion summary after the loop.

---

## Cadence Model (Production Reference)

At `SYNC_INTERVAL_HOURS=4` (6 runs/day):

| DETAIL_BATCH_SIZE | Cases/day | Cycle length | Run time (est.) |
|-------------------|-----------|--------------|-----------------|
| 30 (default)      | 180       | ~14 days     | ~90 s/run       |
| 60                | 360       | ~7 days      | ~180 s/run      |
| 85                | 510       | ~5 days      | ~255 s/run      |

Start at 30 and tune upward after observing PJUD rate behavior in production logs.  
The `_oldest_unchecked_label` log line provides the visibility needed to tune this safely.

---

## Robustness Rule (Exception Handling Model)

| Error type | Timestamp advanced? | Rationale |
|------------|---------------------|-----------|
| Success (0+ movements) | YES | Standard mark-checked |
| Case-specific error (`except Exception`) | YES | Prevents queue starvation — a permanently-failing case rotates to the back |
| Session error (`SessionExpiredError`, `SessionNotAuthenticatedError`) | NO | System-level condition; not the case's fault. Case retains its rotation position for next run |

The `except (SessionExpiredError, SessionNotAuthenticatedError)` clause is positioned BEFORE the generic `except Exception` clause (verified at sync_service.py lines 1434 vs 1500).

---

## Rotation Engine Coverage

This change extends detail-scraping coverage to **all** case data types:

| Data type | Covered by rotation? |
|-----------|---------------------|
| Movements | YES (was always in detect_and_sync_movements) |
| Entities | YES (fetched in same get_case_detail call) |
| Documents | YES (downloaded synchronously during same per-case fetch, 1-hour token window honored) |

The rotation engine now ensures EVERY case for a given lawyer × competencia eventually receives a full detail scrape (movements + entities + documents) over each configured cycle.

---

## Accepted Runtime-Observable Unknowns

These were accepted as non-blockers at planning time and remain open for production tuning:

1. **PJUD rate-limit behavior at larger batch sizes**: unknown undocumented limits. Mitigation: env-tunable `DETAIL_BATCH_SIZE` + `DETAIL_FETCH_DELAY`; start at 30/2.0 and monitor `_oldest_unchecked_label` logs.
2. **Session lifespan in production**: mid-batch expiry frequency is unknown. Mitigation: Slice 2 reauth_callback handles it; logs will reveal frequency.
3. **Per-lawyer vs. total case count (2524)**: it is not confirmed whether ~2524 is per-lawyer or the total across all lawyers. Resolution: the Slice 2 `_oldest_unchecked_label` log provides per-lawyer coverage data. Tune `DETAIL_BATCH_SIZE` after observing real cycle time.

---

## WARNING-1 Resolution (Spec Wording Fix)

The original spec scenario "Timestamp is not updated on fetch error" was too broad — it stated that `last_detail_checked_at` MUST NOT be updated for any fetch error. The implementation correctly distinguishes:

- **Case-specific errors** (generic `Exception`): timestamp IS advanced (starvation prevention).
- **Session errors** (`SessionExpiredError`, `SessionNotAuthenticatedError`): timestamp NOT advanced (case retried next run).

The spec was corrected at archive time:
- `openspec/specs/detail-rotation/spec.md` — the canonical main spec — contains the corrected two-scenario wording.
- The archived delta spec (`openspec/changes/archive/2026-06-12-sync-rotacion/specs/detail-rotation/spec.md`) also contains the corrected wording.
- The original `openspec/changes/sync-rotacion/specs/detail-rotation/spec.md` (in the active change folder) retains the original wording and is superseded by the main spec.

No code change was made — the implementation was already correct. This was a documentation-only correction.

---

## Stale Checkbox Reconciliation

The original `tasks.md` summary table lacked `[x]` markers for S1-T1 through S1-V1 (formatting inconsistency; S2 rows had them). The archived `tasks.md` has all 13 rows marked `[x]`. Evidence:

- `verify-report` (#549): "All 13 tasks marked complete in apply-progress" with explicit per-task table.
- Test evidence: 646 tests pass, 0 failures, 7 new tests added (S1: 1 test file + 4 tests; S2: 6 slice2 tests), mypy clean, migration round-trip clean.

Reconciliation performed at archive time per `sdd-archive` stale-checkbox policy.

---

## Spec Sync Summary

| Domain | Action | Main Spec Location |
|--------|--------|--------------------|
| detail-rotation | Created (new domain) | `openspec/specs/detail-rotation/spec.md` |

The `detail-rotation` domain is new — no prior main spec existed. The corrected spec was written directly to `openspec/specs/detail-rotation/spec.md`.

---

## Cleanup Note for Orchestrator

The original active change folder at `openspec/changes/sync-rotacion/` must be removed as part of the archive commit:

```bash
git rm -r openspec/changes/sync-rotacion/
```

The archived copy at `openspec/changes/archive/2026-06-12-sync-rotacion/` is the authoritative audit trail.

---

## Verification Summary

| Gate | Result |
|------|--------|
| Full non-integration suite | 646 passed, 0 failures |
| Slice 2 only (`-m slice2`) | 6 passed, 0 failures |
| mypy app/core | Success: no issues in 4 source files |
| alembic upgrade head | OK (006 → 007) |
| alembic round-trip (down → up) | Clean |
| CRITICAL issues | 0 |
| WARNING issues | 1 (resolved at archive — spec wording only) |

---

## SDD Cycle Complete

Change `sync-rotacion` has been fully planned, implemented, verified, and archived.  
The rotation engine is live on `feat/sync-rotacion`. Ready for the next change.
