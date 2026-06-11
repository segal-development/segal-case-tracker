# Segal Case Tracker — Roadmap to Autonomous Production

Goal: a professional CaseTracking that scrapes PJUD **3–4×/day automatically**, keeps
every case fresh (movements + full detail + documents), detects changes, and notifies —
deployed on **GCP**.

## Auth model (decided)

| When | Method | Captcha? | Who logs in |
|------|--------|----------|-------------|
| 🌙 Night (autonomous worker, 3–4×/day) | **Clave Única** | No | The worker, using stored encrypted Clave Única credentials |
| ☀️ Day (user-initiated) | **PJUD password** | Yes (reCAPTCHA v3) | The lawyer, solving the captcha invisibly via the frontend |

`Lawyer.preferred_auth_method` drives which path the autonomous worker uses (`clave_unica`).
No 2Captcha needed — Clave Única has no captcha.

## What's already built (the engine — on `main`)

Login → unified session → worker finds it by lawyer_id → autonomous re-auth via encrypted
creds (#9 Slice 3) → movement detection → **full case detail (5 tabs)** parse + persist +
change detection + notifications (causa-detail-full-scrape) → API exposes it all.
The scheduler (`app/workers/sync_scheduler.py`) is wired to sync movements + detail entities.

---

## Blocks (attack in order)

### Block 1 — `auth-autonoma` (the enabler) 🔴 critical path
Make the autonomous nighttime Clave Única login work end-to-end, and confirm the daytime
captcha path.
- **Done**: `_reauth` in sync_scheduler (Clave Única branch), `encrypted_clave_unica_password`
  / `clave_unica_rut` / `preferred_auth_method` columns, `ClaveUnicaAuth.login` flow.
- **Needed**: store each lawyer's Clave Única credentials (encrypted) — a secure onboarding/
  settings flow; **validate `ClaveUnicaAuth.login` end-to-end live** (never tested against a
  real Clave Única session — high risk, mirror the captcha-login bugs we found); ensure the
  worker routes to `clave_unica` for autonomous runs; confirm the daytime captcha path
  (already working) coexists.
- **Risk**: Clave Única redirect flow + session restoration may have the same bugs we hit on
  the captcha path (storage_state, navigation). Validate with a live capture first.
- Est: ~2–3 days (+ a real Clave Única test account).

### Block 2 — `sync-rotacion` (all cases fresh)
Today the worker checks only **5 cases per lawyer per run, no rotation**
(`MOVEMENT_CHECK_DEFAULT_MAX`, GitHub #21). For 2524 cases that means most are never checked.
- **Needed**: a rotation/prioritization strategy so every case is refreshed over time — e.g.
  oldest-`last_checked_at` first, or recently-active-first, with a per-run budget and a
  `last_detail_checked_at` column; throttle (delay between detail fetches) to not hammer PJUD;
  make the per-run cap a tunable.
- Resolves GitHub #21 item 1.
- Est: ~1–2 days.

### Block 3 — `documentos` (PDFs + anexos)
Pull and store the actual documents, not just their tokens.
- **Needed**: download the PDFs behind the doc tokens (movements `Doc.` column, escritos
  `Doc.`, Texto Demanda, Certificado de Envío, Ebook); the **Anexos** and **receptor info**
  load via AJAX with PJUD JWT tokens (`anexoCausaCivil`, `receptorCivil` — deferred phase 2);
  store files in **GCS** (`google-cloud-storage` dep already present); a Documents API +
  link/download endpoints; wire into the sync flow. `download_movement_documents` +
  `Document` model exist as a starting point.
- Est: ~3–4 days.

### Block 4 — `deploy-gcp` (24/7 production)
Run the scheduler worker 24/7 + the API on GCP.
- **Needed**: containers for API + worker (Dockerfile exists, compose has both); **Cloud Run**
  (API) + a long-running **worker** (Cloud Run jobs / GKE / a scheduled Cloud Run); **Cloud
  SQL** (Postgres), **Memorystore** (Redis), **Secret Manager** for keys (Fernet
  `ENCRYPTION_KEY`, SECRET_KEY, SendGrid, Clave Única creds) — deps already present
  (`google-cloud-secret-manager`); Alembic migrations on deploy; **Cloud Scheduler** to drive
  the 3–4×/day cadence (or the in-process APScheduler in the worker); CI/CD; monitoring/alerts.
- Est: ~2–4 days.

---

## Cross-cutting (alongside the blocks)

- **Security close-out**: rotate the PJUD/Clave Única passwords; purge git history of the old
  hardcoded credentials (`git filter-repo`/BFG + force-push); production key management via
  Secret Manager. Credential storage is Fernet-reversible by design (worker must replay the
  password) — restrict + rotate the key.
- **Hardening**: notification config (SendGrid key, webhook endpoints); the notification
  outbox + at-most-once exposure (#21); rate-limiting to PJUD; structured logging + GCP
  monitoring; handle PJUD session fragility (sessions invalidate fast — observed).
- **Validation gaps**: escritos parser against a real case with pending writs; multi-cuaderno
  (cases with several cuadernos only get the default today).

## Rough timeline

- **Critical path to "fresh data with movements + full detail, running autonomously"**:
  Block 1 + Block 2 + Block 4 ≈ **~1 week** (gated on a Clave Única test account + GCP setup).
- **Full "professional + complete" (with documents + hardening)**: ≈ **~2–3 weeks** total.

## Sequencing

1. **Block 1 (auth-autonoma)** — nothing autonomous works without it. Start here.
2. **Block 2 (rotacion)** — so all cases get covered, not just 5.
3. **Block 4 (deploy-gcp)** — turn it on 24/7. (Can start infra in parallel with Block 2.)
4. **Block 3 (documentos)** — the richest enhancement; ships after the core loop is live.

Each block becomes its own SDD change (proposal → spec → design → tasks → apply → verify →
archive) when attacked, following the same disciplined flow used for `#9` and
`causa-detail-full-scrape`.
