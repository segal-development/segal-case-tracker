# Runbook: duplicate `cases` cleanup (T5b)

> **STATUS: NOTHING IN THIS DOCUMENT HAS BEEN EXECUTED.** It is the plan for a
> future real run and needs Marcelo's explicit go before step 1. Spec:
> `odd/tasks/duplicados-por-abogado.md` (T5a produced the report, T5b1 the batched
> `run_case_merge`, T5b2 the runner `scripts/dedup_cases_run.py`). The runner is
> built and tested on SQLite only; it has never touched real data.

**Goal:** remove the surplus `cases` rows that share `(rol, court_id,
coalesce(procedure,''))` (measured 2026-10-21: ~2,945 groups, ~3,083 surplus rows,
all from different lawyers) without losing information.

**Why batches:** `run_case_merge` over every group in ONE transaction costs ~40
queries per loser; at 135 ms per round trip through the Cloud SQL proxy that is
hours inside a single transaction. `scripts/dedup_cases_run.py` runs it N groups
per transaction, committing each batch, and a re-run resumes. The report prints
the exact estimate for the real data (`estimate` in the JSON).

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
merge, i.e. `scripts/dedup_cases_run.py` (section 5).

## 0. Batched merge and runner (implemented, NOT executed)

`run_case_merge(db, *, group_limit, only_group_keys)` exists (T5b1, #396): one call
merges N groups, verification and re-own are scoped to the batch, and a re-run
resumes with the remaining groups. The runner `scripts/dedup_cases_run.py`
(T5b2, `app/services/dedup_cases_runner.py`) drives it. Guards, all enforced in
code:

- **Dry-run by default.** Without `--apply` nothing is written and `run_case_merge`
  is never called: it prints the plan (groups, batches needed, first batch keys).
- **`--backup-ref` is mandatory with `--apply`** (the id of the verified backup, step 2).
- **Quiescent database.** `--apply` refuses (nothing written) if `max(movements.created_at)`
  or any `sync_history.started_at` / `completed_at` is within the last 10 minutes.
  This is the code behind "stop the worker" (section 3).
  `--i-know-the-worker-is-running` disables it and prints a loud warning: do not use it.
- **Typed confirmation.** It shows how many groups the first batch will merge and
  requires typing `MERGE`. `--yes` skips the prompt but still needs `--backup-ref`
  and the quiescence check.
- **First error stops the run.** The failing batch (including
  `CaseMergeVerificationError`) is rolled back alone, earlier batches stay
  committed, there is no retry and no skipping ahead.
- **No progress stops the run** (a batch that merges 0 groups while groups remain).
- **Orphan check** after the last batch and every 10 batches: one aggregate
  `LEFT JOIN cases ... WHERE cases.id IS NULL` query per table of
  `CHILD_TABLES_WITH_CASE_ID` (13 queries); any orphan stops the run.

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
a `windowStartTime` after the command above. Write down its `id`: it is the
`--backup-ref` of the runner. **No backup id, no run** (the runner refuses `--apply`
without it). `<INSTANCE>` and connection data: see
`docs/runbooks/unificar-modelo-causas-migration.md`.

## 3. Worker stance: STOP the worker (recommended)

Stop it for the whole run (`docker compose -f docker-compose.yml -f docker-compose.qa.yml stop worker`);
restart it only after the section 6 verification. The runner enforces this: it
refuses to `--apply` while movements or sync history were written in the last
10 minutes, so wait at least that long after stopping the worker.

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

## 5. Pass 2: merge the non-empty groups with `scripts/dedup_cases_run.py`

All commands from the repo root, with the production `DATABASE_URL` configured as
for the report. Replace `<BACKUP_ID>` with the id from step 2.

```bash
# (a) Dry-run (default): prints the plan and writes nothing.
PYTHONPATH=. poetry run python scripts/dedup_cases_run.py --batch-size 25

# (b) First real run: ONE small batch, to time it and check the result.
PYTHONPATH=. poetry run python scripts/dedup_cases_run.py \
  --batch-size 5 --max-batches 1 --apply --backup-ref <BACKUP_ID> --json /tmp/dedup-run-1.json
# Type MERGE when asked. Then re-run the report and the section 6 queries.

# (c) Full run, sized from (b): `avg_seconds_per_group` in /tmp/dedup-run-1.json x
#     ~2,945 groups = total time. Stop-file and time budget bound each session.
PYTHONPATH=. poetry run python scripts/dedup_cases_run.py \
  --batch-size 25 --apply --backup-ref <BACKUP_ID> \
  --stop-file /tmp/dedup-stop --time-budget-minutes 120 --json /tmp/dedup-run-2.json
```

Per-batch output: `lote N: grupos=.. perdedoras_borradas=.. filas_reasignadas=.. restantes=.. seg=..`.

**Stop gracefully:** `touch /tmp/dedup-stop` from another shell. The runner checks
the file before each batch, finishes the batch in flight, and stops
(`stopped_because: stop_file`). Remove the file before the next run
(`rm /tmp/dedup-stop`). The time budget is also checked only between batches.

**Resume:** run the same command again. Merged groups are no longer duplicates, so
the next batch starts from the first remaining group; nothing is merged twice or
skipped. This is also how you continue after a stop, a time budget or an error.

**On error** (`stopped_because: error`, `integrity_failed` or `no_progress`): only
the failing batch was rolled back and earlier batches stay committed. Do NOT retry
blindly: read `errors` in the JSON summary, investigate, and if committed data
looks wrong restore the backup (section 7).

Exit code is 0 for dry-run, completed, `max_batches`, `stop_file` and `time_budget`,
and 1 for any refusal or error stop.

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
- [ ] Worker stopped (the runner's quiescence guard passes without `--i-know-the-worker-is-running`); extension write freeze decided.
- [ ] Pass 1 delete script reviewed (or pass 1 skipped); runner dry-run output (5a) reviewed.
- [ ] First small apply (5b) done and verified before the full run (5c).
- [ ] Time window agreed using the report's `estimate` (hours at 135 ms/round trip).
- [ ] Post-pass verification queries and rollback owner named.
