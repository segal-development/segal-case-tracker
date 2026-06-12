# detail-rotation Specification

## Purpose

Defines rotation-aware selection of cases for per-run detail scraping, ensuring full-cycle coverage across all cases over a configurable multi-day window. Replaces the hardcoded front-of-list cap for scheduled runs.

## Requirements

### Requirement: Rotation-Aware Case Selection

The system MUST select cases for detail scraping using DB ordering: `last_detail_checked_at ASC NULLS FIRST, filed_at DESC`, capped at `DETAIL_BATCH_SIZE`. NULL (never-checked) cases come first; among equal timestamps, newer-filed cases come first.

#### Scenario: Never-checked cases precede previously-checked cases

- GIVEN cases exist where some have `last_detail_checked_at = NULL` and others have non-null values
- WHEN the rotation selection runs
- THEN all NULL cases appear before any non-null case in the returned batch

#### Scenario: Secondary sort by filed_at DESC within same timestamp group

- GIVEN two cases both have `last_detail_checked_at = NULL`
- WHEN the rotation selection runs
- THEN the case with the more recent `filed_at` is returned first

#### Scenario: Batch is capped at DETAIL_BATCH_SIZE

- GIVEN the DB has more eligible cases than DETAIL_BATCH_SIZE
- WHEN the rotation selection runs
- THEN exactly DETAIL_BATCH_SIZE cases are returned

---

### Requirement: Mark Checked After Successful Detail Fetch

The system MUST set `last_detail_checked_at` to the current UTC timestamp after each successful case detail fetch, regardless of whether new movements were found.

#### Scenario: Timestamp advances on zero-movement fetch

- GIVEN a case whose detail fetch succeeds with 0 new movements
- WHEN the fetch completes without error
- THEN `last_detail_checked_at` is updated to now for that case

#### Scenario: Timestamp is not updated on fetch error

- GIVEN a case whose detail fetch raises an exception
- WHEN the exception is handled
- THEN `last_detail_checked_at` MUST NOT be updated for that case

---

### Requirement: Full-Cycle Coverage (No Starvation)

Given the rotation runs repeatedly, every case MUST eventually have its `last_detail_checked_at` advanced. No case MUST be permanently excluded from selection.

#### Scenario: All cases covered over sufficient runs

- GIVEN a DB with C cases and DETAIL_BATCH_SIZE = B
- WHEN ceil(C / B) consecutive rotation runs complete without error
- THEN every case has a non-null `last_detail_checked_at`

---

### Requirement: Configurable Batch Size and Fetch Delay

`DETAIL_BATCH_SIZE` and `DETAIL_FETCH_DELAY` MUST be readable from environment variables with defaults (30 and 2.0 respectively). `DETAIL_FETCH_DELAY` replaces the previously hardcoded 1.0 s inter-case delay.

#### Scenario: Env overrides are respected at runtime

- GIVEN `DETAIL_BATCH_SIZE=50` and `DETAIL_FETCH_DELAY=3.0` are set in the environment
- WHEN a scheduled sync run executes
- THEN at most 50 cases are detail-scraped AND each inter-case pause is 3.0 s

---

### Requirement: Isolation from Case-List Refresh

The full case-LIST refresh MUST run on every scheduled sync, unchanged. Only the rotation-selected cases undergo detail scraping (movements, entities, documents).

#### Scenario: List refresh is always full regardless of batch size

- GIVEN a scheduled sync run
- WHEN the sync executes
- THEN all cases for the lawyer × competencia are upserted from the PJUD list
- AND only DETAIL_BATCH_SIZE cases receive a detail scrape

---

### Requirement: ROL-Targeted On-Demand Path Unchanged

`POST /sync` with a specific ROL MUST detail-check only that case using the existing lookup. The rotation selection function MUST NOT be invoked for this path.

#### Scenario: On-demand sync targets a single case

- GIVEN a POST /sync request with a specific ROL
- WHEN the sync executes
- THEN only that case's detail is fetched
- AND `last_detail_checked_at` is updated for that case only

---

### Requirement: Empty DB Graceful Fallback

If no Case rows exist for a given lawyer × competencia, the rotation selection MUST return an empty result without raising an exception.

#### Scenario: No cases in DB returns empty, no crash

- GIVEN the DB has no Case rows for the target lawyer and competencia
- WHEN the rotation selection runs
- THEN an empty collection is returned AND no exception propagates

---

### Requirement: Document Download Within Same Per-Case Fetch

Documents MUST be downloaded synchronously during the same per-case detail fetch, honoring the ~1-hour token validity window. No deferred download queue is introduced by this change.

#### Scenario: Documents downloaded before the next case begins

- GIVEN a case with pending document tokens returned by get_case_detail
- WHEN the per-case fetch loop processes this case
- THEN all document downloads are attempted before fetching the next case in the batch

---

## Slice 2 Requirements — Mid-Batch Re-Auth

> Deferred to Slice 2. MUST NOT block Slice 1 delivery. Mark tests with `@pytest.mark.slice2`.

### Requirement: Mid-Batch Session Re-Auth [SLICE 2]

If `SessionExpiredError` is raised during a case's detail fetch, the system MUST invoke a `reauth_callback` (injected at call site), update the active session, and retry that case once. If re-auth fails, the batch MUST stop gracefully without an unhandled exception; remaining cases are deferred to the next scheduled run.

#### Scenario: Re-auth succeeds and case is retried [SLICE 2]

- GIVEN the PJUD session expires mid-batch on case N
- WHEN `SessionExpiredError` is caught and `reauth_callback` returns a valid new session
- THEN the session reference is updated AND case N is retried with the new session
- AND the batch continues with subsequent cases

#### Scenario: Re-auth fails, batch stops gracefully [SLICE 2]

- GIVEN `SessionExpiredError` is raised and `reauth_callback` also raises or returns None
- WHEN the re-auth failure is caught
- THEN the current batch stops processing further cases
- AND no unhandled exception propagates
- AND cases not yet processed retain their previous `last_detail_checked_at` (not marked as checked)
