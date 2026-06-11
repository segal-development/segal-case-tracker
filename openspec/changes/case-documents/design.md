# Design: Case Documents (Block 3) — Slices 1–2

## Technical Approach

Center on the **1-hour JWT window**: tokens are parsed and persisted (Slice 1),
then downloaded synchronously inside the SAME sync task immediately after
`get_case_detail`, while the browser cookies are still alive (Slice 2). All five
static document types are normalized into ONE carrier (`PJUDDocRef`) so a single
generic downloader and a single `Document` table cover them. Disambiguation of
the two `dtaCert` endpoints is by **form action**, never by param name. A
`doc_type` + downloader-strategy seam leaves Slice 3 (AJAX) addable without
touching the core. Idempotency is keyed on a **stable business identity**, not
the volatile JWT (see ADR-1).

## Architecture Decisions

### ADR-1: Idempotency key = stable identity, NOT the raw JWT
**Choice**: `pjud_token_hash = sha256(doc_type | case_rol | scope_key)` where
`scope_key` is the movement natural key (movement/escrito docs) or empty
(case-level docs). Store the live JWT separately in `pjud_token` (refreshed each
sync). Skip download when a `Document` with the same `pjud_token_hash` is already
`stored`.
**Alternatives**: hash the full JWT (proposal's literal wording); content-hash
the downloaded PDF.
**Rationale**: the JWT embeds per-load `iat`/`exp` and a rotating encrypted
`data` claim, so it changes on EVERY sync — hashing it would dedupe only within
one page load and re-download everything forever. A stable identity preserves the
proposal's INTENT (idempotency) where its literal form would break it.
PDF-content hashing still forces the expensive re-download. **This is the single
most important deviation; flagged as a risk + open question.**

### ADR-2: One normalized carrier `PJUDDocRef(doc_type, endpoint, param_name, token, available)`
**Choice**: extend the existing `PJUDDocument` with optional `doc_type`,
`endpoint`, `param_name`; add `PJUDCaseDetail.case_documents: List[PJUDDocument]`
for the 3 case-level types. Movement principal docs and escrito-cert live in
`PJUDMovement.documentos`.
**Alternatives**: a brand-new dataclass hierarchy; per-type dataclasses.
**Rationale**: reuses one dataclass and the existing `download_document` path;
minimal churn; the generic downloader keys off `endpoint`+`param_name`.

### ADR-3: Token authority on `Document`; movement column is the bug-fix denormalization
**Choice**: `Document.pjud_token`/`pjud_token_hash` are authoritative for
download + idempotency. Also add `Movement.document_token` (proposal-mandated bug
fix) as a convenience flag/denormalization.
**Rationale**: keeps download logic single-sourced while satisfying the explicit
"token no longer dropped" success criterion and cheap `has_document` checks.

### ADR-4: Storage behind a backend interface, GCS + Local
**Choice**: `StorageBackend` Protocol (`upload`, `signed_url`, `exists`) with
`GCSStorageBackend` (google-cloud-storage, ADC/`GCS_BUCKET`) and
`LocalStorageBackend` (filesystem, dev/test). `StorageService` wraps the backend
+ idempotency. Factory selects by `settings.GCS_BUCKET` (set → GCS, else Local).
**Rationale**: mockable, lets dev/test run with zero GCP infra, isolates the only
external dependency. Key layout: `cases/{case_id}/{doc_type}/{pjud_token_hash}.pdf`.

### ADR-5: Synchronous in-window download via browser fetch (httpx as future seam)
**Choice**: a `DocumentDownloader` runs inside `detect_and_sync_movements` after
entity persistence, iterating this case's `pending` Documents, reusing the live
page via the generic `download_document` (page.evaluate fetch), throttled by the
existing `TokenBucketLimiter`, each download isolated in try/except.
**Alternatives**: deferred queue (fails — JWT expires); cookies+`httpx` now.
**Rationale**: the proven browser-fetch path reuses live auth cookies and avoids
new auth plumbing. cookies+`httpx` (for large binaries) is left as a
`DocDownloadStrategy` swap, not built in Slice 2.

### ADR-6: `fa-ban` / disabled icon → persist `unavailable`, never download
**Rationale**: the sample shows `Anexos`/`Cert. de Envío` as `fa-ban`. Detect the
disabled icon at parse time → no token, `status='unavailable'`, skipped by the
downloader. Avoids wasted fetches and false failures.

## Data Flow

    get_case_detail ──→ parse static tokens ──→ persist Documents (status=pending)
       (live page)            │                         │
                              │                    [Slice 1 ends here]
                              ▼                         ▼
                    PJUDDocRef list          DocumentDownloader (Slice 2)
                                                        │ throttle → fetch (live cookies)
                                                        ▼
                                          StorageService.upload → GCS  (skip if hash stored)
                                                        │
                                                        ▼
                                        Document.status=stored, storage_uri set
                                                        │
                              GET /cases/{id}/documents · GET /documents/{id} → 302 signed URL

## Token map (from `/tmp/detail_rich.html`)

| doc_type | endpoint (form action) | param | scope |
|----------|------------------------|-------|-------|
| `movement` (docuS/docuN) | `documentos/docuS.php` \| `docuN.php` | `dtaDoc` | movement |
| `escrito_cert` | `documentos/docCertificadoEscrito.php` | `dtaCert` | movement |
| `texto_demanda` | `documentos/docu.php` | `valorEncTxtDmda` | case |
| `cert_envio` | `documentos/docCertificadoDemanda.php` | `dtaCert` | case |
| `ebook` | `documentos/newebookcivil.php` | `dtaEbook` | case |

The two `dtaCert` rows are disambiguated ONLY by form `action`.

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `app/scrapper/pjud/base.py` | Modify | `PJUDDocument` += `doc_type/endpoint/param_name`; `PJUDCaseDetail` += `case_documents` |
| `app/scrapper/pjud/civil.py` | Modify | Parse case-level + escrito-cert tokens (form-action anchored); generic `download_document(endpoint,param)`; `fa-ban` detection |
| `app/scrapper/pjud/selectors/civil.yaml` | Modify | New selectors: `cert_envio/texto_demanda/ebook/escrito_cert` action+param patterns, `disabled_doc_indicator` |
| `app/models/document.py` | Modify | += `doc_type, pjud_endpoint, pjud_token, pjud_token_hash(unique), storage_uri, status`; nullable `movement_id, escrito_id` |
| `app/models/movement.py` | Modify | += `document_token` column (bug fix) |
| `alembic/versions/xxxx_*.py` | Create | One additive, nullable migration |
| `app/services/storage_service.py` | Create | `StorageBackend` Protocol, `GCS`/`Local` backends, `StorageService`, factory |
| `app/services/document_downloader.py` | Create | In-window download orchestrator + `DocDownloadStrategy` seam |
| `app/services/document_persistence.py` (or in sync_service) | Create | Parse-result → `Document` upsert keyed on `pjud_token_hash` |
| `app/services/sync_service.py` | Modify | After entity sync: persist Documents, then run downloader |
| `app/api/v1/documents.py` | Create | `GET /cases/{id}/documents`, `GET /documents/{id}` (302 signed URL) |
| `app/config.py` | Modify | `GCS_BUCKET`, `GCS_SIGNED_URL_TTL`, `DOC_DOWNLOAD_ENABLED` |

## Interfaces / Contracts

```python
class StorageBackend(Protocol):
    def upload(self, data: bytes, key: str, content_type: str) -> str: ...   # → uri
    def signed_url(self, key: str, expires_s: int) -> str: ...
    def exists(self, key: str) -> bool: ...

class DocDownloadStrategy(Protocol):           # Slice 3 seam
    async def fetch(self, scraper, session, doc: "PJUDDocument") -> bytes: ...
# StaticFormStrategy (Slice 2)  ·  AjaxStrategy (Slice 3, not implemented)

def document_identity_hash(doc_type: str, case_rol: str, scope_key: str = "") -> str
```

`Document.status` ∈ `{pending, stored, failed, unavailable}`.
API: `DocumentResponse{id, doc_type, status, filename, size_bytes, document_date, available}`;
`GET /documents/{id}` → 302 to signed URL, 404 if not owned, 409 if not `stored`.

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | parse 5 types, `dtaCert` disambiguation, `fa-ban`→unavailable | fixture `/tmp/detail_rich.html` → `tests/fixtures/` |
| Unit | `document_identity_hash` stable across rotated JWTs | two JWTs, same identity → same hash |
| Unit | StorageService idempotency | `LocalStorageBackend` + mock GCS client; stored hash → no upload |
| Unit | downloader failure isolation + throttle | mock `page.evaluate`, one raises → others proceed; assert limiter acquired |
| Integration | persist → download → serve | mock fetch + StorageService; assert `stored`, signed-URL 302 |

No live downloads anywhere.

## Migration / Rollout

One additive Alembic migration; all new columns nullable (except `status` default
`pending`). `pjud_token_hash` gets a unique index for upsert/idempotency.
Rollback: `alembic downgrade -1`; GCS uploads idempotent → no data loss.
`DOC_DOWNLOAD_ENABLED=false` ships Slice 1 (persist only) without Slice 2 downloads.

## Slice 3 seam (do NOT implement)

`doc_type` enum reserves `anexo_causa`, `receptor`, `receptor_reserva`;
`Document.pjud_endpoint` nullable so AJAX docs persist as `unavailable` until
traced. New AJAX types = new parser entry + `AjaxStrategy` only — no core change.

## Open Questions

- [ ] ADR-1: confirm the encrypted `data` JWT claim is NOT stable across syncs
      (if it IS, identity hashing is still safe but JWT hashing would also work).
- [ ] Per-cuaderno scope for `texto_demanda`/`ebook` — one row per case, or per cuaderno?
- [ ] Signed-URL TTL and whether to stream-proxy instead of 302 redirect.
