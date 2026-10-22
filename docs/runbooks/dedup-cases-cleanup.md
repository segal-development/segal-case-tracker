# Runbook: duplicate `cases` cleanup (T5b)

> **STATUS: NOTHING IN THIS DOCUMENT HAS BEEN EXECUTED.** It is the plan for a
> future real run and needs Marcelo's explicit go before step 1. Spec:
> `odd/tasks/duplicados-por-abogado.md` (T5a produced the report, T5b is the run).

**Goal:** remove the surplus `cases` rows that share `(rol, court_id,
coalesce(procedure,''))` (measured 2026-10-21: ~2,945 groups, ~3,083 surplus rows,
all from different lawyers) without losing information.

**Why two passes:** `run_case_merge` processes every group in ONE transaction at
~40 queries per loser; at 135 ms per round trip through the Cloud SQL proxy that
is hours inside a single transaction. The report prints the exact estimate for the
real data (`estimate` in the JSON).

## Measured on the real database (2026-10-22, read-only report, nothing executed)

`scripts/dedup_cases_report.py` against production: 2,945 duplicate groups, 3,086
surplus rows (2,814 groups of 2 copies, 131 of 3+). **EMPTY surplus rows: 0.
SOURCE_ONLY: 0. NON-EMPTY: 3,086.** Every surplus row carries `cartera_snapshots`
(6,117 rows) and `case_lawyer_source` (3,288 rows), and most carry `alerts` (28,157),
so there is **no zero-risk "pass 1" on today's data**: the earlier estimate of ~1,968
rows deletable with no loss only looked at movements, deadlines and alerts. The only
safe way to remove a surplus row is the merge (`run_case_merge`), which already
dedupes snapshots by period, sightings by lawyer and alerts by
(lawyer, movement, type, message). Movements on surplus rows: 25,118, of which 24,284
already exist on the winner by (folio, description) and 834 (205 groups) would be
unioned into it. Estimated cost of the existing merge in one transaction: 4.63 h.

Consequence: skip pass 1 as written below unless it is redefined to carry the
sighting and the snapshots to the winner first; plan the real run as the batched
merge (section 0 is therefore REQUIRED, not optional).

## 0. Required follow-up before pass 2 (NOT implemented)

`run_case_merge(db)` takes no batch parameter. Pass 2 needs a **later PR** adding
`group_limit` and/or `only_group_keys` to `run_case_merge` (and to
`_find_duplicate_identity_groups`) so one call merges N groups and commits, and a
re-run resumes with the remaining groups. Until that PR is merged, only pass 1 can run.

## 1. Preconditions (all must be true)

- [ ] T3c (#394) is deployed and the worker ran at least one **full cycle** with the new code.
- [ ] Duplicates stopped growing. Run the read-only report twice, one full cycle apart
      (`PYTHONPATH=. python scripts/dedup_cases_report.py --json /tmp/dedup-before.json`
      and again later): `totals.groups` and `totals.surplus_rows` of the second run
      must be **<=** the first.
- [ ] `risk.groups_spanning_court_or_competencia == 0` in the report.
- [ ] Review the report's `groups` for `risk.groups_winner_changes_owner` and the
      `loser_classification.source_only` count (see pass 1).

## 2. Backup (rollback primary)

```bash
gcloud sql backups create --instance=<INSTANCE> --description="pre-dedup-cases-cleanup"
gcloud sql backups list --instance=<INSTANCE> --limit=3
```

Verification that the backup exists: the new entry shows `status: SUCCESSFUL` and
a `windowStartTime` after the command above. Write down its `id`. **No backup id,
no run.** `<INSTANCE>` and connection data: see
`docs/runbooks/unificar-modelo-causas-migration.md`.

## 3. Worker stance: STOP the worker (recommended)

Stop it for the whole run (`docker compose -f docker-compose.yml -f docker-compose.qa.yml stop worker`);
restart it only after pass 2 verification.

Why: the worker's sync reads and writes `cases`, `movements`, `documents` and
`case_lawyer_source` for the same rows the cleanup deletes or repoints. A sync that
adds a movement to a loser between the report and the delete loses that movement
(pass 1 would also delete a row that stopped being empty); a sync creating a row
under the firm lawyer meanwhile changes the groups. Keeping it running is only
acceptable for pass 1 if each delete re-checks emptiness inside its own statement
(see pass 1) AND the API/extension ingest is also considered; the default is to stop.
The extension ingest (`ingest_service`) writes to the same tables through the API:
announce a short write freeze or accept that its writes during the window are
covered only by the emptiness re-check.

## 4. Pass 1: delete EMPTY surplus rows only (zero information loss)

Scope: losers in `loser_classification.empty` (zero rows in **every** table of
`CHILD_TABLES_WITH_CASE_ID`). Size: `empty_only_first_pass_rows`.

- Tier `source_only` (only `case_lawyer_source` rows, created for each case's own
  lawyer by the T3a backfill) is NOT empty: deleting it loses the lawyer
  sighting. Either carry the sighting to the winner first (what
  `_backfill_case_lawyer_source` does) or leave it for pass 2. Decide with the
  report numbers before the go.
- Implementation of the delete is out of scope here: it must be a script reviewed
  separately, in batches (e.g. 500 ids per transaction), each DELETE guarded by
  `NOT EXISTS` over every child table so a row that gained data is skipped, and the
  ids taken from a fresh report, never from an old JSON.

## 5. Pass 2: merge the non-empty groups in resumable batches

Requires the section 0 follow-up. Run `run_case_merge` with N groups per
transaction (start small, e.g. N=25, and time one batch to confirm the estimate),
commit per batch, repeat until the report shows 0 groups. A batch that raises
`CaseMergeVerificationError` rolls back only that batch; stop and investigate.

## 6. Verification after EACH pass

```sql
-- (a) duplicate groups: pass 1 -> fewer than before; after pass 2 -> must be 0
SELECT count(*) FROM (
  SELECT 1 FROM cases WHERE competencia = 'civil'
  GROUP BY rol, court_id, coalesce(procedure, '') HAVING count(*) > 1) g;

-- (b) orphan child rows: must be 0 for EVERY table with a case_id FK
--     (movements, documents, case_litigantes, case_notificaciones, case_escritos,
--      case_exhortos, case_deadlines, alerts, hitos, liberacion_requests,
--      generated_documents, cartera_snapshots, case_lawyer_source)
SELECT count(*) FROM movements t LEFT JOIN cases c ON c.id = t.case_id WHERE c.id IS NULL;

-- (c) counts before/after: after >= max of any single copy (the winner keeps its
--     rows and gains the unioned ones), per table
SELECT count(*) FROM movements;   -- also documents, hitos, alerts, case_deadlines
```

Compare with the `/tmp/dedup-before.json` totals: `movements` after must be
`before - movements_contained_total` (pass 2 only; pass 1 must leave it unchanged),
`hitos`, `alerts`, `liberacion_requests`, `generated_documents`, `cartera_snapshots`
must not decrease except for the documented dedup collisions
(`uq (periodo, case_id)` snapshots, identical alerts). Any other drop = stop and restore.

Finish with the report again: `totals.groups` is 0 after pass 2.

## 7. Rollback

Primary: restore the Cloud SQL backup taken in step 2 (point-in-time clone or
restore, per the existing Cloud SQL runbook). `reverse_case_merge` is best-effort
only: it recreates skeleton `cases` rows from `case_merge_audit` and does NOT split
re-pointed or deduped children back. Do not rely on it.

## 8. Go / no-go checklist for Marcelo

- [ ] Report numbers reviewed (groups, surplus, empty vs source_only vs non_empty, unioned movements).
- [ ] Duplicate count not growing over one full worker cycle.
- [ ] Backup id recorded and `SUCCESSFUL`.
- [ ] Worker stopped; extension write freeze decided.
- [ ] Pass 1 delete script reviewed; section 0 PR merged before pass 2.
- [ ] Time window agreed using the report's `estimate` (hours at 135 ms/round trip).
- [ ] Post-pass verification queries and rollback owner named.
