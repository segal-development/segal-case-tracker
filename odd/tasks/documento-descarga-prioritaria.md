# Documento: descarga prioritaria al pedirlo
"Ver documento" on a document not yet downloaded must not fail: it queues the document first for the station and tells the user it will be ready in minutes.
Backend: /Users/marcelo/Projects/segal-case-tracker. Frontend: /Users/marcelo/Projects/segal-deudores-web.

## Specs
- S1. Marcelo (19-11-2026, verbatim): "Da error cuando quiero ver el documento" (screenshot: causa C-10371-2023, "Último documento del PJUD · Resolución 02/11/2023 · Ver documento" → "No se pudo abrir el documento"; console `GET /api/v1/documents/293986/download 409 (Conflict)`).
- S2. Measured in prod (19-11): doc 293986 is `status='pending'`, no gcs_path, has pjud_endpoint + pjud_token. Documents: 112,445 stored, 254,207 pending, 4,676 failed, 25 unavailable. `GET /documents/{id}/download` (app/api/v1/documents.py ~278) serves stored docs from storage; otherwise it tries a live PJUD download with the REQUESTING user's PJUD session in Redis and returns 409 "No active PJUD session — login again" when there is none (admins/most users), or 409 "Document has no PJUD token" without a token.
- S3. Decision (Marcelo, 19-11-2026, "Dale" to option A): every PJUD request keeps going out from the station, never from the server. When a document is not stored, the API marks it for priority download (a timestamp of the request, idempotent; keep the first request time) and answers a clear, distinct status the front can show — not a generic error. The station's document downloader takes priority-requested pending documents first (oldest request first) before its normal backlog, every cycle, within its existing budget/limits.
- S4. Front: on that status, "Ver documento" shows "Este documento aún no se descarga del PJUD. Lo pedimos con prioridad; estará disponible en unos minutos." (no red error); a second click re-checks and opens it once stored. Documents without a PJUD token keep a clear message ("Este documento no se puede descargar: falta actualizar la causa.") instead of the generic error. Stored documents open exactly as today.
- S5. Visibility rules unchanged (404 for documents outside the caller's case scope). failed/unavailable documents: a priority request may re-queue a `failed` one once (respecting the downloader's retry rules); `unavailable` keeps its message.

## Tasks
- T1 S2, S3, S5 backend + station downloader — route: worker.
- T2 S4 frontend — route: worker after T1 contract.
- T3 PRs, deploy, verify with doc 293986 — route: parent.

## Log
- L1. Marcelo's report with screenshot, 19-11-2026 (S1).
- L2. Diagnosis in prod (S2) and Marcelo's "Dale" to option A (S3).
