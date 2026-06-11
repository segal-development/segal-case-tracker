# Proposal: async-pipeline-notifications

## Context

The product's core promise is: **when a new movement appears in a lawyer's PJUD case, notify them automatically.** Today the scraping + diff layer works (`SyncService` upserts cases/movements and creates `Alert` rows), but **nothing is actually sent** — `NotificationService` is fully stubbed.

**Deadline**: a working end-to-end demo must be shown to management on **Friday 2026-06-12** (proposal written 2026-06-10 — 2 days). The goal beyond the demo is production-grade unattended operation, but that is explicitly a *later* milestone.

Decision (locked with the user): the Friday demo runs with **manual login at start** + the full flow (scrape → detect new movement → email + webhook). Unattended auto re-login is **deferred** to production hardening.

## Problem

1. `NotificationService.send_email_alert` / `send_webhook` / `notify_new_movement` are stubs — no notification is ever delivered.
2. **Verified gap**: `POST /sync` (`sync.py:86`) only calls `sync_cases()`. It does NOT fetch case details or call `sync_movements()`, so no movements are processed and `alerts_created` is always 0. The on-demand flow cannot currently produce the event that should trigger a notification.
3. Webhook CRUD endpoints (`/webhooks`) are mocks, so there is no way to register a destination.

## Scope

### A) DEMO SLICE — must work flawlessly by Fri 2026-06-12

1. **Close the movement-detection gap**: extend the on-demand sync flow so that after syncing the case list it fetches case details (movements) and runs `sync_movements()`, creating `Alert` rows. Use the existing `fetch_case_details_parallel` helper; for demo reliability, support scoping to a small set (e.g. a single `rol` or first N cases) so the live run is fast and deterministic.
2. **Implement `NotificationService.send_email_alert`** via SendGrid (`SENDGRID_API_KEY`, `FROM_EMAIL`). On success set `alert.email_sent=True, email_sent_at=now()`. If `SENDGRID_API_KEY` is empty → log warning and skip (never raise).
3. **Implement `NotificationService.send_webhook`** — HMAC-signed POST: header `X-Webhook-Signature: sha256=<hmac(webhook.secret, body)>`. Best-effort (single attempt for the demo), record outcome; set `alert.webhook_sent=True` on 2xx.
4. **Wire `notify_new_movement()`** into `sync_movements()` immediately after `_create_movement_alert` (`sync_service.py:202`), dispatching email + webhook for the lawyer.
5. **Minimal webhook CRUD** (`/webhooks`): at least `POST` (create, auto-generate `secret`) and `GET` (list), so a destination can be registered for the demo.

### B) PRODUCTION HARDENING — after the demo (out of scope for Friday)

- **Auto re-login** in the scheduler using stored credentials: Clave Única (no captcha) and 2Captcha for the captcha flow — replaces the current "skip lawyers without a session" behavior (`sync_scheduler.py:70,84`). This is the real keystone for unattended operation.
- **Session-expiry fallback**: try auto re-login first; only if it fails → pause (`needs_login`) AND email the lawyer to reconnect.
- **Robust webhook delivery**: `WebhookDelivery` table (per-attempt tracking), retry with backoff (+1m/+5m/+30m/+2h/+12h), deactivate webhook after N consecutive failures.
- **Deduplication**: DB unique constraint on `alerts(movement_id, type)` to avoid double notifications when scheduled + manual sync overlap.
- **Scheduler hardening**: per-lawyer try/catch isolation; enabled by config.
- **On-demand job queue** (DB-backed, Postgres `SELECT ... FOR UPDATE SKIP LOCKED`) — only if/when the on-demand API use case is prioritized. Chosen over GCP Pub/Sub (zero new infra, exact-once, fits sync SQLAlchemy).

## Non-Goals

- GCP Pub/Sub queue.
- On-demand job queue (deferred to a later change).
- Unattended auto re-login for the Friday demo (production milestone).

## Key Decisions

- **Manual login at demo start** (lower risk for the live demo); auto re-login is production work.
- **Both channels** for v1: email (to `Lawyer.email`) + webhook.
- **Webhook consumer = Segal frontend**; payload `{event, data}`, versioned.
- **HMAC signing from day 1** (cheap, correct); retry/backoff/delivery-tracking is production hardening.
- **DB-backed queue** over Pub/Sub — when the queue is eventually built.

## Webhook Payload Contract (v1, for Segal frontend)

```json
{
  "event": "movement.created",
  "version": "1",
  "data": {
    "lawyer_id": 0,
    "case": { "rol": "C-7615-2026", "tribunal": "...", "caratulado": "..." },
    "movement": { "folio": "3", "fecha": "08/06/2026", "descripcion": "Acredita Poder", "etapa": "..." }
  }
}
```

Signature: `X-Webhook-Signature: sha256=HMAC_SHA256(webhook.secret, raw_request_body)`.

## Risks & Demo Notes

- **Detail-fetch latency**: fetching movements for many cases is slow. Mitigate with `fetch_case_details_parallel` and demo-scoping to a single `rol` / first N cases.
- **First sync fires many alerts**: on a case's first sync, all its movements are "new" → notifications fire. Good for the demo (run /sync on a fresh case → emails fire).
- **Live validation needs the user**: real PJUD credentials, a real inbox to receive the email, and a webhook receiver (e.g. a webhook.site URL) for the demo. To be set up before the rehearsal.
- **PJUD session 2h TTL**: fine for the demo (manual login at start).
- **`SENDGRID_API_KEY`** must be set in the demo environment or email silently skips.

## Demo Rehearsal Checklist (Thu 2026-06-11)

1. Set `SENDGRID_API_KEY`, `FROM_EMAIL`, `ENVIRONMENT=development`.
2. Register a webhook destination (webhook.site) via `POST /webhooks`.
3. Login (`POST /pjud/login`) → get `session_id`.
4. `POST /sync` scoped to one `rol` → verify: movements detected, alert created, email received, webhook POST received with valid signature.
5. Dry-run the full sequence twice end-to-end.

## Delivery

- Demo slice fits in one focused PR (`fix/security-hardening` is independent; build on `main`/a new branch). Given the deadline, compress planning and go straight to TDD implementation in small reviewable commits.
- Production hardening becomes follow-up changes after the demo.

## Next

`sdd-spec` / `sdd-design` may be compressed given the deadline — recommend a lean task list and immediate implementation of the demo slice.
