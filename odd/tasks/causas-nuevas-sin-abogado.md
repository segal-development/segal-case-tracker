# Causas nuevas sin abogado demandado

Once a day, the station searches PJUD's public consulta unificada (as Invitado, "Búsqueda por Fecha") for new civil causas.
It keeps C causas whose demandado has no lawyer, and stores ROL, tribunal, carátula and the demand's text. The team sees them on a page reached through a secret link, without logging in.
Backend: segal-case-tracker, branch `feat/causas-nuevas-sin-abogado`. Front: segal-deudores-web.

## Specs

- **S1 — Date search, civil only.**
  - The scraper takes a desde/hasta date range, competencia Civil, and a tribunal. It runs the public "Búsqueda por Fecha" of oficinajudicialvirtual.pjud.cl as Invitado.
  - Scope: the civil tribunals of the Corte de Santiago and the Corte de San Miguel (Marcelo: "Solo Santiago y San Miguel (recomendado)"). The tribunal list must be extendable later without a code change.
  - Quote: "La idea es poder enviar una fecha desde y hasta, solo civil y que pueda escocger el tribunal, y todas las cortes."
- **S2 — Once a day, previous business day.**
  - The daily run searches the previous business day, using the existing business-day calendar.
  - Marcelo chose "El día hábil anterior (recomendado)".
  - Quote: "Esto se haria una vez al dia".
- **S3 — Only C roles.** Results whose ROL does not start with "C" are discarded. Quote: "A nosotros solo nos sirven los roles que empiezan con la letra C."
- **S4 — No lawyer for the demandado.**
  - For each C result, the scraper opens the detail (the lupa) and reads the Litigantes tab.
  - The causa is kept only if no litigante is a lawyer of the demandado side (see L8 for the exact codes).
  - Quote: "vas a Litigantes para ver que Litigante no tenga abogado demandado, porque si no eso quiere decir que tiene a alguien que lo defienda".
- **S5 — What is stored.**
  - For each kept causa: ROL, tribunal, carátula, and the extracted text of the demand.
  - The demand PDF is read but never stored. Marcelo: "Texto completo extraído (recomendado)".
  - Quote: "dentro del modal se extrae el ROL, tribunal , Caratula y descargar el texto de la demanda, o mas que descargar leer eso ya que como es de uso publico.. seria ocupar espacio de mas".
  - The same causa (ROL + tribunal) is never stored twice.
- **S6 — Page without login, secret link.**
  - A page lists the causas found each day, with the date, ROL, tribunal, carátula and a way to read the demand text.
  - It is reached through a long secret link, without logging in. Marcelo chose "Link secreto sin sesión" after being told anyone holding the link sees the data.
  - An admin can rotate the link, which invalidates the old one.
  - The page is not indexable (noindex) and only lists what the link grants.
- **S7 — PJUD footprint.**
  - Every request goes out from the station, through the egress guard (5G allow-list) and the PJUD pacing/rate limiter.
  - Every request is counted in `pjud_request_counts` under its own kind.
  - A daily cap on detail and document reads stops the run cleanly when reached; the remainder waits for the next run.
  - The run skips the 03:30–04:30 window, like incorporation.

## Tasks

| ID | Specs | Route | Commit |
|----|-------|-------|--------|
| T0 | S1, S3–S5 | parent, live capture with Playwright from Marcelo's home IP (L7) | done 27/11 |
| T1 | S1–S5, S7 | worker, backend scraper + daily job, built on T0 fixtures (after T0) | |
| T2 | S5 (storage), S6 (API) | worker, backend: migration 094, models, public + admin endpoints | 7764daf |
| T3 | S6 (UI) | worker, front: public page + admin link card | front 68b3854 |

### Data + API contract (T1 ↔ T2 ↔ T3)

- Migration 094:
  - `pjud_demandas_publicas`: id, rol, tribunal (name), court_id FK courts nullable, corte, caratulado, fecha_ingreso date, texto_demanda Text nullable, litigantes JSON, encontrada_at. UNIQUE(rol, tribunal).
  - `pjud_busqueda_fecha_runs`: id, fecha date, corte, status (`running|done|partial|failed`), contadores JSON, started_at, finished_at. UNIQUE(fecha, corte).
  - `public_link_tokens`: id, purpose (`demandas_publicas`), token_hash (sha256, unique), created_at, created_by lawyer_id, revoked_at, last_used_at.
- `GET /api/v1/publico/demandas/{token}?desde=YYYY-MM-DD&hasta=YYYY-MM-DD&q=&page=1&per_page=50`, no auth.
  - Returns `{ items: [{ id, rol, tribunal, caratulado, fecha_ingreso, encontrada_at, tiene_texto }], total, page, per_page }`, newest `fecha_ingreso` first.
  - An invalid or revoked token → 404 `{"detail": "Link no válido."}`, the same for both cases.
- `GET /api/v1/publico/demandas/{token}/{id}` → `{ id, rol, tribunal, caratulado, fecha_ingreso, encontrada_at, texto_demanda }`. 404 as above.
- Both public responses carry `X-Robots-Tag: noindex, nofollow` and `Cache-Control: no-store`. The token is never logged.
- Admin (`require_admin`):
  - `GET /api/v1/admin/demandas-publicas/link` → `{ activo, creado_at, last_used_at }`. Never returns the token.
  - `POST` same path → `{ token }`. Creates a new link and revokes the previous one; the token is shown only here.
  - `DELETE` same path → 204, revokes the active link.
- Front public route `/demandas/:token`, outside the login guard.

## Log

- **L1 (27/11, Marcelo, verbatim):** "Che locura.... me han pedido un nuevo scrape pero esta vez es la parte mas publica del PJUD , que seria aqui : [captura 3.57.23 p.m.] La idea es poder enviar una fecha desde y hasta, solo civil y que pueda escocger el tribunal, y todas las cortes. Esto se haria una vez al dia , una vez se haya obtenido los datos como te uestro aca : [captura 3.59.51 p.m.] A nosotros solo nos sirven los roles que empiezan con la letra C. Una vez lo tengas, le haces click a la lupita y los datos que muestra el modal vas a Litigantes para ver que Litigante no tenga abogado demandado, porque si no eso quiere decir que tiene a alguien que lo defienda, y dentro del modal  se extrae el ROL, tribunal , Caratula y descargar el texto de la demanda, o mas que descargar leer eso ya que como es de uso publico.. seria ocupar espacio de mas... me explico? Se entiende lo que se quiere hacer?"
- **L2 (decisions):**
  - Tribunales: "Solo Santiago y San Miguel (recomendado)".
  - Demanda: "Texto completo extraído (recomendado)".
  - Ventana: "El día hábil anterior (recomendado)".
- **L3 (Marcelo, verbatim, on where to see results):** "Quizas hacer una pantalla dentro del sistema pero sin que se incie sesion.. se entiende? que solamente las personas que lo van a usar lo hagan.. porque lo otro seria hacer un ejecutable de Pyhton o no?"
  - Follow-up choice: "Link secreto sin sesión".
- **L5 (decision, parent):** the daily run covers every calendar day since the last successful run up to yesterday, so a Monday run also covers the weekend and no ingress day is skipped. This meets "el día hábil anterior" without gaps.
- **L6 (code map):** reuse `civil.py` consulta (`_consulta_fetch_modal`, `_parse_litigantes_table` :767, `_parse_search_results_html` ~:1360, `download_document` :918), `pdf_text.extraer_texto_pdf`, `pjud_action_limiter(kind).acquire()` (counts metrics), `egress_allowed`, the `_maybe_run_sysgal_incorporacion` / `_maybe_sync_sysgal_cuotas` scheduler patterns, `courts.pjud_corte`, and the `hito_form_links` / `sysgal_api_keys` token patterns.
  - Unknowns that need live HTML (T0): the date-search POST fields, tribunal iteration, results paging, reCAPTCHA for Invitado, how the demandado's lawyer appears in Litigantes, and how the demand document is linked.
- **L7 (T0 live capture, 27/11, Playwright from Marcelo's home IP as Invitado; raw files in `~/segal-diag/pjud_pub/`, outside the repo, contain real personal data, never commit them):**
  - Navigation: `indexN.php` → `accesoConsultaCausas()` → tab "Búsqueda por Fecha" (`#formConsultaFec`) → `#fecCompetencia`=3 → `#corteFec` (90 = C.A. de Santiago, 91 = C.A. de San Miguel; 0 = Todos) → `#fecTribunal` (loaded per corte, e.g. 259 = 1º Juzgado Civil de Santiago; 33 options for Santiago incl. 2 placeholders) → `#fecDesde` / `#fecHasta` `dd/mm/yyyy` (readonly datepickers, set with jQuery `.val()`) → click `#btnConConsultaFec`.
  - The form carries a reCAPTCHA v3 token (`g-recaptcha-response-fecha`, `action=validate_captcha_fecha`) generated by the page. The scraper must drive the real UI and click Buscar, never replay a raw POST.
  - The page then POSTs `ADIR_871/civil/consultaFechaCivil.php` (`fecDesde, fecHasta, fecCompetencia=3, fecTribunal, corteFec`).
  - The response is bare `<tr>` rows: lupa `detalleCausaCivil('<JWT, ~30 min>')`, Rol (`C-15839-2026`, also `E-967-2026`), Fecha `dd/mm/yyyy`, Caratulado, Tribunal. All rows come in one response; paging is client-side.
  - Volume: 1º Civil de Santiago 03–26/11/2026 = 75 rows, 21 of them C (about 1 C per day per court).
  - Modal `#modalDetalleCivil`: tabs Historia, Litigantes, Notificaciones, Escritos por Resolver, Exhortos. The Litigantes table has the columns `Participante | Rut | Persona | Nombre o Razón Social`.
    - Codes seen: `DTE.`, `AB.DTE`, `AP.DTE`, `DDO.`, `AB.DDO` (e.g. "… (Sin Acreditacion)"), `DDOR.` (debtor in a concursal case).
  - Demand text: a form posting to `ADIR_871/civil/documentos/docu.php` with input `valorEncTxtDmda`. The POST returns `application/pdf` (229 KB); `extraer_texto_pdf` gave 20,106 chars.
- **L8 (decision, parent; told to Marcelo):** S4 "sin abogado demandado" = the causa has at least one `DDO.` litigante and no litigante whose code starts with `AB.DDO` or `AP.DDO`. Causas without a `DDO.` (e.g. a voluntary liquidation with only a `DDOR.` / solicitante) are not kept.
- **L9 (decision, parent, after T1 first pass):** the daily cap counts every modal opened in the Chile day, across runs and restarts. The days searched for a corte are every day in the last 7 days up to yesterday that has no `done` run, and never earlier than the first run recorded for that corte; the first run searches yesterday only. This refines L5: a `partial` or `failed` day is retried, and a weekend is still caught up.
- **L10 (live smoke, 27/11):** the driver was run from the Mac against the real page (1º Civil de Santiago, 25–26/11): 31 Santiago tribunals listed, 6 rows / 2 C, both modals parsed (codes AB.DDO, AP.DDO, DDOR.), demand PDFs read (12,469 and 20,106 chars). Two driver fixes came out of it: open the detail by calling the page's `detalleCausaCivil(token)` after hiding and emptying the previous modal (a shown modal intercepted the lupa click), and clear `#fecTribunal` before choosing the corte (Civil preloads all 230 civil courts of the country).
- **L4 (screenshots):** `~/segal-diag/capturas/pjud_pub_1.png` (search form: Desde/Hasta, Competencia Civil, Tribunal, Corte Todos) and `pjud_pub_2.png` (results Rol/Fecha/Caratulado/Tribunal with a lupa; a reCAPTCHA badge is visible).
