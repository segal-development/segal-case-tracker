# Egress guard: allow-list (only the 5G)

The station must query PJUD only through the 5G modem. Today the guard is a deny-list (`FORBIDDEN_EGRESS_IPS`), so any new network passes.
Backend: segal-case-tracker, branch `feat/egress-lista-permitida`.

## Specs

- **S1 — Allow-list.** When an allow-list is configured, scraping is allowed only if the current egress IP matches one of its entries. Entries are exact IPs or CIDR ranges (IPv4/IPv6).
  - Any other IP → scraping refused, the same way a forbidden IP is refused today (same code path, same pause, same Telegram alert flow).
  - Unknown IP (lookup failed) → refused (fail closed, unchanged).
- **S2 — Where the list lives.**
  - Primary source: a DB app setting (the same mechanism the station already reads for remote settings, e.g. `app_settings` / goals), so an admin can change it remotely without touching the station.
  - Fallback: env `ALLOWED_EGRESS_IPS` (comma-separated, same parsing as `FORBIDDEN_EGRESS_IPS`).
  - Both empty → behaviour identical to today (deny-list only). No deploy may stop the station by itself.
  - The DB value must not be cached for longer than the existing 5-minute guard cache.
- **S3 — Deny-list still applies.** An IP in `FORBIDDEN_EGRESS_IPS` is refused even if it is also in the allow-list.
- **S4 — Alert text.** When refused because the IP is not in the allow-list, the Telegram alert says so and names the IP, e.g. `"El worker se detuvo: la IP 181.42.38.231 no está en la lista de redes permitidas."`. The existing recovery alert stays: `"El worker volvió a una red permitida (IP …)."`.
- **S5 — Remote admin.** `GET /api/v1/admin/estacion/ips-permitidas` → `{ "ips": [...] }`; `PUT` with `{ "ips": [...] }` saves the list. Admin only (others 403). Each entry is validated as an IP or CIDR; an invalid entry → 422 naming it and nothing saved. An empty list is allowed (turns the allow-list off).
- **S6 — Initial value.** After deploy the parent sets the list to `["186.9.145.83"]` in prod (with Marcelo's OK and a backup).

## Tasks

| ID | Specs | Route | Commit |
|----|-------|-------|--------|
| T1 | S1–S5 | worker, then independent verifier (Risk: item 2, guard) | |
| T2 | S6 | parent, prod write after deploy | |

## Log

- **L1 (27/11, Marcelo, verbatim):** "Dale entonces habria que hacer ese cambio entonces" — replying to the proposal: "en vez de una lista de IPs prohibidas, una lista de permitidas (solo el 5G). Así, si la estación cambia de red por cualquier motivo, se frena sola."
- **L3 (decision, parent):** `goals.value` is Integer-only, so the allow-list gets a new text settings table (migration 093, `get_text`/`set_text`), stored as one comma-separated value under `estacion_ips_permitidas`. A DB read error falls back to env and is logged. Deploy needs a Cloud SQL backup first (migration).
- **L2 (evidence):** from 27/11 04:00 Chile all PJUD requests left from 181.42.38.231 (AS27651 Entel Chile fixed, same AS as the blocked office IP 186.10.251.189) instead of the 5G 186.9.145.83 (AS27925 Entel PCS). The deny-list let it through.
