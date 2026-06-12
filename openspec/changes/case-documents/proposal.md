# Proposal: Case Documents (Block 3)

## Intent

The scraper captures JWT document tokens during `get_case_detail` but **never persists them**, and no PDF is ever downloaded or served. Users get case metadata and movements but cannot access the actual legal documents (resolutions, writs, demanda text, certificates, ebook). This change brings the real PDFs into the system: parse + persist all static tokens, download them to GCS, and serve them via API. **Driving constraint:** PJUD document JWTs expire **1 hour after page load** (`exp - iat = 3600s`), so downloads cannot be deferred to a later job — they must run synchronously inside the same sync task, immediately after `get_case_detail`.

## Scope

### In Scope
- Parse + persist all static document tokens: movement docs (`docuS`/`docuN`), escrito cert (`docCertificadoEscrito`), texto demanda (`docu` / `valorEncTxtDmda`), certificado de envío (`docCertificadoDemanda`), ebook (`newebookcivil`).
- Fix existing bug: movement `documento_token` is captured but silently dropped — add column + persist it.
- `StorageService` (GCS): idempotent upload keyed by `pjud_token_hash` (SHA-256), signed-URL serving.
- Synchronous download of static PDFs inside the sync flow, within the 1h window, reusing `download_document` (page.evaluate fetch with auth cookies).
- Documents API: list per case + per-document download/signed-URL.

### Out of Scope (Non-Goals / Blocked)
- AJAX documents — `anexoCausaCivil`, `receptorCivil`, `receptorCivilReserva`: endpoints UNKNOWN, **BLOCKED on a live network trace** (later slice).
- Laboral / penal jurisdictions; multi-cuaderno documents.
- Residual: escritos tab parser still unvalidated on real data (samples have no pending writs).

## Capabilities

### New Capabilities
- `case-documents`: parse, store (GCS), and serve case PDFs downloaded from PJUD.

### Modified Capabilities
- `case-sync`: sync flow now downloads + persists documents within the token TTL window.

## Approach

Center everything on the **1-hour synchronous-download** constraint. During each sync, after `get_case_detail`: (1) parse all static tokens into the PJUD dataclasses; (2) persist tokens + `doc_type` + `pjud_token_hash` to DB; (3) immediately download each static PDF via the existing browser-context fetch and upload to GCS, skipping any already-stored hash. The page session and cookies are alive only during the sync, so download must reuse that context (perf option: extract cookies + `httpx` for large binaries to avoid the `Array.from(Uint8Array)` JS→Python round-trip). `fa-ban` / disabled icons mean unavailable → store `None`, never attempt download. `dtaCert` collides between escrito and envío certs → differentiate by endpoint, not param name.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/scrapper/pjud/civil.py` | Modified | Parse all new token types |
| `app/scrapper/pjud/base.py` | Modified | Extend dataclasses with case-level + cert tokens |
| `app/models/document.py` | Modified | Add `doc_type`, `pjud_token_hash`, `escrito_id` |
| `app/models/movement.py` | Modified | Add `document_token` column |
| `alembic/versions/` | New | Migration |
| `app/services/storage_service.py` | New | GCS upload/download/signed URL |
| `app/services/sync_service.py` | Modified | Persist tokens + trigger in-window download |
| `app/api/v1/cases.py` | Modified | Documents list + download endpoints |
| `app/scrapper/pjud/selectors/civil.yaml` | Modified | New endpoint selectors |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| 1h JWT expiry | High | Download synchronously in same sync task; no deferred queue |
| `dtaCert` name collision | Med | Differentiate by endpoint, store `doc_type` |
| Unknown AJAX endpoints | High | Slice 3 blocked until live trace; do not spec by assumption |
| GCS not configured | Med | Provision `GCS_BUCKET` + service account before Slice 2 |
| PJUD rate-limiting on bulk downloads | Med | Reuse `rate_limiter.py`, 1–2s/request delay |
| Large-PDF memory (JS round-trip) | Med | Offer cookies+`httpx` path for binaries |
| Conditional availability (`fa-ban`) | Med | Detect disabled icon → null token, skip download |
| Ebook generation latency | Med | Timeout + retry; may degrade gracefully if slow |

## Rollback Plan

Revert the Alembic migration (`alembic downgrade -1`) and revert the code changes. New columns are additive/nullable and GCS uploads are idempotent, so no data loss on existing cases. Document API endpoints are net-new and can be removed without affecting case/movement sync.

## Dependencies

- GCS bucket + service-account credentials (`GCS_BUCKET` env) before Slice 2.
- Live network trace of AJAX functions before Slice 3 (blocking).

## Suggested First Slice

**Slice 1 — Token capture + DB persistence + migration (no downloads).** Lowest risk, fixes the dropped-token bug, unblocks everything else without GCS infra. Slice 2 adds StorageService + in-window download + API. Slice 3 (AJAX) stays blocked until traced.

## Success Criteria

- [ ] All 5 static token types parsed and persisted with `doc_type` and `pjud_token_hash`.
- [ ] Movement `document_token` no longer dropped (bug fixed).
- [ ] Static PDFs downloaded to GCS within the 1h window during sync; idempotent on re-sync.
- [ ] `GET /cases/{id}/documents` lists documents; per-document endpoint returns a signed URL.
- [ ] `fa-ban`/disabled documents handled as unavailable without errors.
