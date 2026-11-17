# Novedades agrupadas
Reduce the noise in Novedades: group activity alerts by causa and day, and paginate on the server so the list and its counter agree. Audit finding #26.
Backend: /Users/marcelo/Projects/segal-case-tracker. Frontend: /Users/marcelo/Projects/segal-deudores-web.

## Specs
- S1. Audit (16-11-2026, verbatim): "Novedades: 155 alertas que requieren atención y 77.603 de actividad. Con ese volumen de ruido, las alertas importantes se pierden." Measured: 72,464 `new_movement` alerts in 30 days vs ~400 plazo alerts. Today one alert per new movement (`sync_service._create_movement_alert`), the "Actividad" tab is flat, and the front loads only page 1 with `per_page: 100` and slices it client-side at 15, while the badge counts everything.
- S2. Decision (Marcelo, 2026-11-16): do grouping + real pagination now; the relevance split (movements that open a plazo or change the stage → "Requiere atención", the rest → "Trámites") is LATER, out of scope here.
- S3. Backend: `GET /api/v1/alerts` accepts `group_by=case_day` (default off: today's flat behaviour unchanged for every caller). With it, the response items are groups keyed by (case_id, Chile calendar day of created_at): `{group_key, case_id, rol, caratula (if available), day ("YYYY-MM-DD"), count, unread_count, latest_created_at, latest_title, latest_message, types: [distinct alert types], alert_ids: [ids, capped at 200]}`, ordered by latest_created_at desc; `total` = number of groups and `unread_count` = number of groups with at least one unread alert, both within the requested category (actionable/activity) and the caller's own alerts, exactly like today's filters (including the existing actionable-category rules). Alerts without case_id form their own group per (type, day). Server pagination `page`/`per_page` applies to groups. Computed in SQL (GROUP BY), no per-alert Python loop over the whole set.
- S4. Mark-as-read for a group: `POST /api/v1/alerts/read-group` with `{group_key}` (or case_id + day + category) marks all the caller's alerts of that group read; existing per-alert read stays.
- S5. Front (`src/screens/Novedades.tsx`, `src/hooks/useAlerts.ts`): the "Actividad" tab uses `group_by=case_day` with server pagination (existing Pagination component, `per_page` 20) — one row per group: ROL (link to the causa), day, "N movimientos nuevos" (or the single title when count = 1), the latest message, unread dot; clicking marks the group read. The tab badge = groups with unread. "Requiere atención" keeps one row per alert but also moves to real server pagination (no 100 cap). The sidebar bell is unchanged.
- S6. Strict TDD both repos (RED seen): grouping keys and Chile-day boundary (UTC vs Chile), counts and unread per group, category filters identical to today's, pagination totals, read-group only touches the caller's own alerts, default (no group_by) response byte-identical to main; front groups render, badge = unread groups, pagination requests the right page, both tabs no longer cap at 100.

## Tasks
- T1 S3, S4, S6 backend — route: worker, branch feat/novedades-agrupadas.
- T2 S5, S6 frontend — route: worker after T1 contract.
- T3 PRs, deploy, check with a real account — route: parent.

## Log
- L1. External audit 16-11-2026, finding #26 (quoted in S1).
- L2. Marcelo (2026-11-16): "Si" to doing 1 (group by causa/day) and 2 (real pagination) now; 3 (relevance split) later.
