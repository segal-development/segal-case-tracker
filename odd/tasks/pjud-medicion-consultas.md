# PJUD: medir consultas y alertar rechazos
Count every request the scraping station makes to PJUD, per hour, per account, per egress IP and per action, and alert on PJUD rejections, so there is daily numeric proof of the station's footprint and an early warning before the 5G IP gets blocked.
Backend: /Users/marcelo/Projects/segal-case-tracker, branch feat/pjud-medicion-consultas.

## Specs
- S1. Marcelo (24-11-2026), verbatim: "esto se puede mejorar o refactorizar para mejorar performance y quitar de una vez por todas que esto no implica nada con respecto al bloqueo PJUD". He then said "Si dale arranca" to step 1 of the proposal:
  > "Medir y alertar. Un contador de cuántas consultas le hacemos al PJUD por hora, por cuenta y por IP, más un aviso si aparece un rechazo, un captcha o una respuesta rara del PJUD."
  Step 2 (list each lawyer's causas once a day instead of every lap) comes later, decided with a week of these numbers.
- S2. Context:
  - The office IP 186.10.251.189 was blocked by PJUD again on 23-11-2026.
  - The station never uses that IP. The egress guard forbids it, and the station leaves through the 5G modem (186.9.145.83 on 23-11).
  - Station load: about 40 lawyer syncs per day, roughly 3 full laps over 13 accounts, 24/7, with 40 to 170 detail opens per hour.
  - Pacing is fixed by `pjud_action_limiter` in app/scrapper/pjud/resilience/rate_limiter.py: login about 1 every 30 s, list about 1 every 10 s, detail about 1 every 2 s, document about 1 every 7 s.
- S3. Counting:
  - Count every PJUD request the station actually sends, by action: login, list, detail, document, plus any other action that goes through the limiter.
  - Hook at the single choke point every PJUD request already passes (the action limiter acquire, or the nearest common wrapper). Never add calls to PJUD.
  - Each count carries the account (`lawyer_id`, null when not applicable) and the current egress IP. Reuse the egress guard's cached lookup; never add lookups.
  - Counts are aggregated in memory and flushed to the DB in hourly buckets (UTC hour) by the station: at least at every lawyer-unit boundary, and at cycle end.
  - Flushing is upsert-safe: a crash loses at most the unflushed counts, and never double counts.
- S4. Rejections:
  - Separately count PJUD answers that signal throttling or blocking: HTTP 403, HTTP 429, a captcha page, the Shape/anti-bot challenge (`ShapeChallengeError`), session-rejected-as-blocked, and repeated connection resets.
  - Reuse the exception classes and detectors the scraper already has. Find them; do not invent new heuristics unless none exist.
  - Each rejection kind is counted with the same dimensions as S3.
- S5. Alert:
  - When rejections appear, send ONE Telegram message using the existing notifier (the same one the incorporation brake uses), with the kind, account, IP and count.
  - Do not alert again for the same IP and kind within 1 hour.
  - No client data and no credentials in the message.
- S6. Storage:
  - New table, migration 091: `pjud_request_counts` with `hour_utc`, `lawyer_id` (nullable), `egress_ip`, `action`, `kind` (request | rejection type), `count`.
  - Unique on (hour_utc, lawyer_id, egress_ip, action, kind).
  - A retention note in the docstring. Rows are tiny and no purge is needed now.
- S7. Read:
  - `GET /api/v1/admin/pjud/actividad?days=7` (admin only; 403 for other roles).
  - Returns totals per day and per hour, by IP, by action, by account, and the rejections.
  - It is admin-only because it carries IPs.
  - No UI in this feature.
- S8. Must not change pacing, budgets, retries, the egress guard, credentials, or any scraping behaviour. Measurement only. If counting fails, scraping continues and the failure is logged once.

## Tasks
- T1 S3-S8 implementation + tests. Route: worker.
- T2 independent verifier (risk: shared hot path in the scraper, concurrency), PR, merge, deploy. Route: parent.
- T3 after one week of data: report to Marcelo, then decide step 2. Route: parent.

## Log
- L1. Marcelo's question and "Si dale arranca", 24-11-2026 (S1).
