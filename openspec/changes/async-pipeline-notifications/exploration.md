# Exploration: async-pipeline-notifications

## Goal

Replace the stubbed async/notification layer with a working pipeline:

1. **Job queue** — API enqueues a scrape request → background worker picks it up → runs the scraper → stores the result; status is pollable.
2. **Notifications** — when sync detects a NEW movement, notify via email (SendGrid) and/or outbound webhook (HMAC-signed, with retry/backoff and delivery tracking).
3. **Scheduler** — drive periodic syncs (already exists via APScheduler, currently OFF by default).

Benchmarked against the sibling system "scalex" (DB-backed `Procesos` queue + cron worker + webhook delivery). We steal the architecture (DB queue, decoupled delivery), not the code (scalex has shell injection, no retry, no HMAC).

## Current State

### Already implemented (we plug into these)

- `app/services/sync_service.py` — `sync_cases()` upserts Case + SyncHistory; `sync_movements()` upserts Movement and calls `_create_movement_alert()` per new movement, creating `Alert(email_sent=False, webhook_sent=False)`. `needs_sync()` checks freshness. **This is where notifications hook in.**
- `app/services/session_store.py` — Redis-backed `PJUDSession` retrieval, keyed by `session_id` and `lawyer_id` (2h TTL).
- `app/workers/sync_scheduler.py` — APScheduler `AsyncIOScheduler`, `IntervalTrigger(hours=4)`; gated by `ENABLE_SCHEDULER`. The Docker `worker` container runs it.
- Playwright scrapers via `get_scraper()` + `BrowserFactory` — fully working (fresh browser per request, session restored from Redis).

### Stubbed / broken

| File | Method | Line | State |
|------|--------|------|-------|
| `app/services/scrapper_service.py` | `create_search_job` / `create_refresh_job` / `get_job_status` / `update_job_status` | 17/37/51/68 | fake job_id, no queue, status always "pending", no-op |
| `app/workers/scrape_worker.py` | `process_refresh_job` / `start` | 73/86 | `NotImplementedError` |
| `app/services/notification_service.py` | `send_email_alert` / `send_webhook` / `notify_new_movement` | 16/30/46 | `NotImplementedError` / no-op |
| `app/workers/notification_worker.py` | `process_webhook` / `start` | 37/50 | `NotImplementedError` |
| `app/api/v1/scrapper.py` | all endpoints | 12–84 | hardcoded responses |
| `app/api/v1/webhooks.py` | all CRUD | 13–100 | empty/hardcoded |

### Confirmed pre-existing bug (to fix as part of this change)

- `app/workers/scrape_worker.py:10,18,41` — instantiates `SessionManager` (keyed by RUT) and calls `get_session(lawyer_id)`, but `SessionManager.get_session()` expects a RUT. Must use `SessionStore.get_session_by_lawyer(lawyer_id)`. Silent auth failure today. (Related to the broader two-session-store fragmentation.)

### Corrected during verification (NOT a bug)

- An earlier exploration draft claimed the `sync_history` migration was missing. **Verified false**: `alembic/versions/002_add_sync_history.py` exists and creates the table. Existing migrations run up to `003_add_clave_unica_fields.py`, so new migrations start at **004**.

## Domain Model

- **`Alert`** (`app/models/alert.py`): `email_sent/email_sent_at`, `webhook_sent/webhook_sent_at`. Adequate for email (single-shot); insufficient for webhook retry (no per-attempt tracking).
- **`Webhook`** (`app/models/webhook.py`): `secret` (HMAC key already present), `failure_count`, `last_triggered_at`, `is_active`. No per-attempt delivery log.
- **`SyncHistory`** (`app/models/sync_history.py`): full run tracking (cases/movements counts, status, duration, triggered_by).
- **`Lawyer`** (`app/models/lawyer.py`): `email` (SendGrid target), `encrypted_pjud_password`.

### Config already present (deps already in pyproject)

`GCP_PROJECT_ID=""` (empty), `GCP_PUBSUB_TOPIC="scrape-jobs"`, `SENDGRID_API_KEY=""`, `FROM_EMAIL`, `REDIS_URL`, `ENABLE_SCHEDULER`. `google-cloud-pubsub` and `sendgrid` are installed but unused.

## Key Architectural Fork: Job Queue Backend

| Criterion | (A) DB-backed (Postgres SKIP LOCKED) | (B) GCP Pub/Sub |
|-----------|--------------------------------------|-----------------|
| New infra | None | GCP project + credentials |
| Duplicate safety | Exact-once (SKIP LOCKED) | At-least-once (needs idempotency) |
| Dev/CI experience | Works with existing docker-compose | Needs emulator or real GCP |
| Async/sync fit | Sync (matches codebase) | Async-preferred (tension) |
| Observability | `SELECT * FROM scrape_jobs` | GCP console |
| Effort | Medium | High |

**RECOMMENDATION: (A) DB-backed queue.** The system processes at most hundreds of jobs/day. A `scrape_jobs` table claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, polled by a second APScheduler job in the existing `worker` container, gives exact-once semantics with zero new infra and fits the sync SQLAlchemy pattern. Pub/Sub is over-engineered here (`GCP_PROJECT_ID` is empty; no local emulator; async/sync bridging cost).

## Notification Approach

- **Email (SendGrid)**: `send_email_alert()` → `SendGridAPIClient`. On success set `alert.email_sent=True`. If `SENDGRID_API_KEY` empty → log warning and skip (do NOT raise, must not break sync).
- **Webhook (HMAC + retry)**: new `WebhookDelivery` table (`webhook_id, alert_id, attempt_number, status, http_status_code, response_body, attempted_at, next_retry_at`). Sign with `X-Webhook-Signature: sha256=HMAC(webhook.secret, canonical_json)`. Retry schedule e.g. +1m/+5m/+30m/+2h/+12h. `NotificationWorker` polls due deliveries. Deactivate webhook after N consecutive failures. Improves on scalex (which has no HMAC, no retry, no status tracking).

## Migration Plan (numbering corrected)

- `004_add_scrape_jobs.py` — DB queue table
- `005_add_webhook_deliveries.py` — retry/delivery tracking

`alembic/env.py` autogenerates from `Base.metadata`.

## Suggested Scope Slicing

- **Slice 1 — Queue pipeline** (~300-400 lines): migration 004 + `ScrapeJob` model; implement `ScrapperService` (DB job + Redis status cache); fix `ScrapeWorker` session bug + implement `start()` poll loop; wire `/scrapper/*` endpoints.
- **Slice 2 — Notification pipeline** (~350-450 lines, depends on Slice 1): `send_email_alert` (SendGrid); `send_webhook` (HMAC); `WebhookDelivery` model + migration 005; `NotificationWorker` retry loop; wire `notify_new_movement()` into `sync_movements()`; implement webhook CRUD.

## Risks

1. **Async Playwright in sync worker** — keep job execution inside the `AsyncIOScheduler` event loop (the scheduler already bridges this).
2. **Redis unavailable** — DB is source of truth; fall back to DB query for status if `get_redis_client()` is None.
3. **PJUD session expiry mid-job** — classify auth failures (mark job `needs_login`, do not retry) vs transient (retry). Reuse the typed `SessionNotAuthenticatedError` from the scraper.
4. **`SENDGRID_API_KEY` empty** — explicit guard, warn-and-skip.
5. **Null webhook secret** — define behavior for active webhooks without a secret (require, or send unsigned + document).
6. **Duplicate alerts** — concurrent scheduled + manual sync could double-create alerts before commit. Consider a DB unique constraint on `alerts(movement_id, type)`.

## Open Questions for Proposal

1. Email the lawyer when their PJUD session expires and sync is paused?
2. Webhook payload schema — wrapped `{event, data}` or flat?
3. Auto-generate webhook `secret` on creation, or optional?
4. Should on-demand `scrape_jobs` also write a `SyncHistory` entry?
5. Are both `search` (find cases by ROL/RUT) and `refresh` (movements for a case) in Slice 1, or defer `refresh`?
6. Is a DB unique constraint on `alerts(movement_id, type)` acceptable?

## Next

Ready for `sdd-propose`. Proposal should formalize the two-slice plan, define the webhook payload contract, and resolve open questions 1–3.
