# Proposal: Sync Detail Rotation (Block 2)

## Intent

The scheduled worker refreshes the full case LIST every run, but only deep-scrapes the DETAIL (movements + entity tabs + documents) of the SAME front-of-list 5 cases every run — `_select_cases_for_movement_check` returns `api_cases[:5]` and the PJUD list exposes no change indicator. Result: ~2519 of 2524 cases NEVER get movements/documents updated. Goal: rotate detail-scraping so EVERY case is refreshed over a multi-day cycle, with never-checked and newer cases prioritized.

## Scope

### In Scope
- Migration 007 + ORM column `cases.last_detail_checked_at` (nullable timestamp).
- Rotation-aware selection (`ORDER BY last_detail_checked_at ASC NULLS FIRST, filed_at DESC LIMIT batch`) replacing the front-of-list cap for scheduled runs.
- Set `last_detail_checked_at` after each successful detail fetch (even with 0 new movements).
- Config knobs: `DETAIL_BATCH_SIZE=30`, `DETAIL_FETCH_DELAY=2.0` (replaces hardcoded 1.0).
- Mid-batch re-auth (Slice 2): `reauth_callback` in `detect_and_sync_movements`; per-case `SessionExpiredError` → reauth → retry. Production-critical.
- Progress/observability logging (coverage X/Y, oldest checked).

### Out of Scope / Non-Goals
- Priority fast-lane for active cases (Approach B) — documented future enhancement.
- Changing the case-LIST refresh (already covers all cases).
- Laboral/penal-specific tuning.
- On-demand `POST /sync` ROL-targeted path stays as-is.
- Document-token 1-hour window stays honored (downloads stay synchronous per-case).

## Capabilities

### New Capabilities
- `detail-rotation`: scheduled selection of which cases get a detail scrape per run, rotation fairness via `last_detail_checked_at`, batch sizing, and mid-batch re-auth.

### Modified Capabilities
- None.

## Approach

**Approach A — simple round-robin** (user-confirmed; B noted as future). DB state is the source of truth for selection (ignores PJUD pagination order). NULLS FIRST guarantees never-checked cases get priority; `filed_at DESC` secondary sort favors newer cases for free. Batch size is env-tunable.

**Cadence model** (6 runs/day at `SYNC_INTERVAL_HOURS=4`, ~2524 cases): cycle days = N / (6 × batch). batch=30 → 180/day → ~14-day cycle (~90s/run); batch=85 → ~5-day cycle. Start conservative at 30, tune upward after observing PJUD behavior.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/models/case.py` | Modified | Add `last_detail_checked_at` column |
| `alembic/versions/007_*` | New | Migration for the column |
| `app/services/sync_service.py` | Modified | Rotation selection fn; set timestamp; reauth retry |
| `app/workers/sync_scheduler.py` | Modified | Wire rotation fn + reauth_callback |
| `app/config.py` | Modified | `DETAIL_BATCH_SIZE`, `DETAIL_FETCH_DELAY` |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| PJUD undocumented rate limits at larger batches | Med | Env-tunable batch + delay; start at 30; monitor |
| Session expires mid-batch | High (at 30+) | Slice 2 reauth_callback + per-case retry |
| Existing test asserts `await_count==5` | High | Update tests to cover rotation selection separately |

## Rollback Plan

Revert the slice PR(s). The migration is additive (nullable column) — `alembic downgrade -1` drops it cleanly; no data loss. Reverting selection logic restores the `[:5]` behavior.

## Dependencies

- None beyond existing scraper/session infrastructure.

## Success Criteria

- [ ] Every case's `last_detail_checked_at` advances within one configured cycle; no case starved.
- [ ] Never-checked cases are picked before previously-checked ones (NULLS FIRST).
- [ ] Timestamp updates even on 0-movement fetches.
- [ ] Slice 2: session expiry mid-batch triggers reauth and the batch continues.
- [ ] Batch size and delay are env-configurable.

## Suggested First Slice

Slice 1 (foundation): migration + ORM column + rotation selection fn + timestamp update + config knobs + wire into `sync_lawyer_cases` + tests. Slice 2 (mid-batch reauth + observability) follows.
