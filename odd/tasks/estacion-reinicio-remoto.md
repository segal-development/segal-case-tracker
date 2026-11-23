# Estación: reinicio remoto (suave)
Let an admin ask the scraping station to reset its in-memory state (circuit breakers, PJUD sessions, rate limiters, browser) without SSH: the station polls for the request; nobody connects to it.
Backend: /Users/marcelo/Projects/segal-case-tracker, branch feat/estacion-reinicio-remoto.

## Specs
- S1. Marcelo (23-11-2026): "Pero no puedes reiniciar tu ?" then "Si hacelo" to the proposal: an admin (or the assistant with his OK) marks "reiniciar la estación" on the server; the station, which already talks to the server every cycle, sees the request and restarts itself. No SSH, no inbound access to the station.
- S2. Why: 17-20/11 the `pjud-detail` circuit breaker got stuck half-open (fixed in #484) and only a manual restart of the `Segal-Worker` task freed it; every restart needed Marcelo at the station. The station SSH key is blocked for the assistant by policy.
- S3. Step 1 (this feature) = SOFT reset, no process exit: on a pending request, at the next safe point of the scheduler (between lawyer units / before a cycle, never mid-case), the station:
  - resets every circuit breaker in `app/scrapper/pjud/resilience/integration.py` `_circuit_breakers`, plus the PJUD action rate limiters/buckets that hold state;
  - drops in-memory PJUD sessions/scrapers so the next unit re-authenticates the normal way;
  - closes the browser if one is open;
  - logs one INFO line, and records that the request was done.
  Hard restart (exit so the task restarts it and pulls new code) is OUT of scope until the Windows task settings are confirmed.
- S4. Request storage in `goals` (same pattern as the other operational switches):
  - `estacion_reinicio_solicitado_at`: epoch seconds, set by the admin endpoint;
  - `estacion_reinicio_hecho_at`: epoch seconds, set by the station after the reset.
  - A request is pending when solicitado > hecho. The station must act at most once per request. It must ignore requests older than its own process start (a fresh process is already clean), but still mark them done.
  - The reset runs only in the scheduler process that scrapes PJUD (the station). The VM worker must not act on it (e.g. gate on the egress guard being satisfied, or on the scraping flags the station already uses — find the existing marker that distinguishes the station from the VM).
- S5. Admin API (`role == admin` only; auditors and lawyers get 403):
  - `POST /api/v1/admin/estacion/reinicio` sets solicitado_at = now and returns `{solicitado_at, pendiente: true}`. Idempotent while pending: it keeps the first time.
  - `GET /api/v1/admin/estacion/reinicio` returns `{solicitado_at, hecho_at, pendiente}`.
  - Both actions are recorded by the existing activity audit.
- S6. Safety:
  - A failure during the reset is logged and never stops the scheduler loop.
  - It never runs mid-case.
  - It does not change pace, budgets, the egress guard or credentials.

## Tasks
- T1 S3-S6 backend + scheduler hook + tests. Route: worker.
- T2 PR, verifier (concurrency/environment), merge, deploy, first real use. Route: parent.
- T3 (later, needs Marcelo) hard restart, after checking the Windows task's restart-on-exit setting.

## Log
- L1. Marcelo, 23-11-2026: "Pero no puedes reiniciar tu ?" / "Si hacelo".
- L2. Parent decision: soft reset first, because the station launcher is not in the repo and whether the task restarts an exited process is unknown.
- L3. Implementation findings, from the worker and an independent verifier (both PASS, no blockers):
  - Two breaker registries are reset: `integration._circuit_breakers` and `circuit_breaker._circuit_breakers`, the latter holding `pjud-detail`. Both limiter registries are reset too.
  - The SessionStore singleton is dropped, and Redis sessions are kept so the login pace is unchanged.
  - Station marker: `PJUD_SCRAPING_ENABLED`.
  - Spec deviations (accepted):
    - (a) No browser-close step. The scheduler holds no long-lived browser between units; browsers are per unit and closed in their own finally.
    - (b) The existing activity audit records only the POST, not the GET.
  - A reset can allow at most one already-configured burst (up to 3 logins, 3 lists, 2 details, 1 document) if the buckets were drained.
