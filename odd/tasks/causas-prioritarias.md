# Causas prioritarias

Each lawyer marks the causas they want followed with priority from PJUD. Marked causas show in their own submenu, and the station reads them more often than the rest.
Backend: segal-case-tracker (`feat/causas-prioritarias`). Front: segal-deudores-web.

## Specs

- **S1 — Mark one or several causas.** A logged-in lawyer can mark one or several of their causas as priority and unmark them. Quote: "cada abogado que entra al sistema, quiere poder seleccionar las csasuas que el o ella encuentra que hay que hacerle seguimiento con prioridad del PJUD, es decir escocgen una o varias y esas se les agrega un estado nuevo para prioridad".
  - The mark is personal: it is stored per (causa, lawyer) and each lawyer sees only their own marks.
  - A lawyer can only mark causas inside their visible scope (`resolve_case_scope`). A causa outside it gets 404.
  - Marking an already-marked causa is idempotent. Unmarking a causa that isn't marked returns 204.
- **S2 — Per-lawyer cap.** The cap is **20 marked causas per lawyer** (Marcelo chose "Hasta 20 por abogado").
  - Going over the cap → 409 with the detail `"Máximo 20 causas prioritarias por abogado."`, and no mark is saved.
  - A bulk request that would exceed the cap is rejected whole: 409, nothing saved.
- **S3 — Priority submenu.** Quote: "que queden en alguna carpeta o algun submenu del sistema".
  - A sidebar item "Prioritarias", with a count, opens a list of the lawyer's marked causas.
  - Each row shows: ROL, carátula, tribunal, semáforo, last PJUD review, the date it was marked, and an action to unmark.
- **S4 — Visible state.** Marked causas show a priority star:
  - in the Causas list, where the existing multi-select gets "Marcar prioritarias" / "Quitar prioridad";
  - in the causa detail header, as a toggle.
- **S5 — The station reads them more often.** Marcelo answered "Sí, más seguido".
  - A marked causa goes to the front of the station's detail rotation when its `last_detail_checked_at` is NULL or older than `DETAIL_PRIORITY_MIN_HOURS` (default 4).
  - There are at most `DETAIL_PRIORITY_MAX_PER_BATCH` (default 10) priority slots per batch, so the normal rotation keeps its floor.
  - The flag `DETAIL_PRIORITY_ENABLED` defaults to true.
  - The mark does NOT override `poda_at` or `DETAIL_MIN_YEAR`.
  - The order relative to existing priorities: requested documents first, then fatal plazo ≤ 2 days, then priority causas, then the age rotation.

- **S6 — "Seguimiento prioritario" screen per Marcelo's design** (`odd/design/seguimiento-prioritario.dc.html` + `.png` in the front repo) replaces the T3 list screen at `/prioritarias`:
  - Breadcrumb "CAUSAS › MI CARPETA", title "Seguimiento prioritario", text "Las causas que marques aquí quedan en esta carpeta con el estado **Prioridad PJUD**. Se revisan primero en cada sincronización y te avisamos de cada movimiento nuevo. La carpeta es personal." Counters at the right: "En seguimiento N" and "Con movimiento nuevo N".
  - Card "Causas en seguimiento" ("N causas · ordenadas por semáforo y último movimiento", top right "Última revisión PJUD: <most recent last_detail_checked_at of the marked causas>"). Columns CAUSA (ROL + black badge "PRIORIDAD PJUD" + carátula), TRIBUNAL, ÚLTIMO MOVIMIENTO (description + date + yellow "Nuevo" pill), SEMÁFORO (Crítica / Atención / Al día pills), "Quitar". Ordered by semáforo (rojo, amarillo, verde, rest) then latest movement desc.
  - Card "Agregar causas" ("Tus causas activas · marca las que quieres seguir con prioridad"): chips "Todas N", "En rojo N", "Con movimiento nuevo N"; search "Buscar por ROL, partes o tribunal"; rows with a checkbox (checked + disabled when already priority), CAUSA, TRIBUNAL, SEMÁFORO, SEGUIMIENTO ("Prioridad PJUD" / "Sin prioridad"); selecting rows marks them (bulk) — the S2 cap and its 409 message still apply.
  - "Nuevo" / "Con movimiento nuevo" = the causa has an unread movement alert (Novedades, `alerts.movement_id` not null and `read_at` null) for the current lawyer. "Te avisamos" is the existing Novedades alerts; no new channel.
  - Wording: the state is called "Prioridad PJUD" everywhere (Causas star tooltip, detail toggle), sidebar item "Seguimiento prioritario".

## Tasks

| ID | Specs | Route | Commit |
|----|-------|-------|--------|
| T1 | S1, S2 (API) | worker, backend: migration 092 `case_priority`, endpoints, `is_priority` | 6ddef96 |
| T2 | S5 | worker, backend station: rotation clause plus config | bb3dcb9 |
| T3 | S3, S4 (+ S1/S2 errors in UI) | worker, front | front 10e057e |
| T4 | S6 (API fields) | worker, backend: `GET /priority-cases` adds `ultimo_movimiento {descripcion, fecha}`, `movimiento_nuevo: bool`, top-level `con_movimiento_nuevo`, `ultima_revision`; `GET /cases` gains filter `movimiento_nuevo=true` and item `movimiento_nuevo` | |
| T5 | S6 (UI) | worker, front | |

### API contract (T1 ↔ T3)

- `GET /api/v1/priority-cases` → `{ items: [{ case_id, rol, caratula, tribunal, semaforo, last_detail_checked_at, marcada_at }], total, max: 20 }`, ordered by `marcada_at` desc.
- `PUT /api/v1/cases/{case_id}/priority` → 204. Errors: 404 when outside scope; 409 when over the cap.
- `DELETE /api/v1/cases/{case_id}/priority` → 204.
- `POST /api/v1/priority-cases/bulk` with body `{ case_ids: int[], on: bool }` → `{ total }`. Errors: 404 when any id is outside scope; 409 when over the cap. Nothing is saved on error.
- `is_priority: bool` is added to the items of `GET /cases` and to `GET /cases/{id}`, for the current lawyer.

## Log

- **L1 (26/11, Marcelo, verbatim):** "me han pedido algo ... Lo que pasa es que cada abogado que entra al sistema, quiere poder seleccionar las csasuas que el o ella encuentra que hay que hacerle seguimiento con prioridad del PJUD, es decir escocgen una o varias y esas se les agrega un estado nuevo para prioridad.. y que queden en alguna carpeta o algun submenu del sistema... me explico?"
- **L2:** Should they be consulted more often in PJUD? → "Sí, más seguido".
- **L3:** Cap per lawyer? → "Hasta 20 por abogado (recomendado)". Every 4 h.
- **L5 (26/11, Marcelo, verbatim):** "Aqui esta el disenio de lo que te pedi anteriormente : '/Users/marcelo/Downloads/Rediseño de interfaz de semáforo-project/README.md'" → the relevant page is "Seguimiento prioritario.dc.html" (copied to the front repo `odd/design/`). Becomes S6.
- **L4 (map):** latest migration is 091. Hook at `sync_service.py:~2330` (`order_clauses[0:0]`). The Causas multi-select already exists (`Causas.tsx:471`, bulk bar ~840-860). Sidebar `getNavGroups` `g-trabajo` (~232).
