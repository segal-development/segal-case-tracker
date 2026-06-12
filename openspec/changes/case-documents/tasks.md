# Tasks: case-documents — Slices 1 & 2

## Metadata

- **Spec**: `sdd/case-documents/spec` (engram obs 531) + `openspec/changes/case-documents/specs/`
- **Design**: `sdd/case-documents/design` (engram obs 532) + `openspec/changes/case-documents/design.md`
- **Test runner**: `.venv/bin/python -m pytest`
- **Verification**: `pytest -m "not integration"` + `mypy app/core` + `alembic upgrade head` / `downgrade -1` / `upgrade head`
- **TDD mode**: STRICT — tests MUST be written and confirmed failing before implementation
- **Delivery strategy**: Chained PRs, stacked-to-main — one PR per slice

---

## Slice 1 — Token Capture + DB + Migration

**PR branch**: `feat/case-documents-slice-1 → main`
**Rollback boundary**: `alembic downgrade -1` (additive-only migration, no data loss)
**Verification gate**: all checks in S1-T14 must be green before opening PR #1

### S1-T1 — Extend HTML test fixtures `[SEQUENTIAL — prerequisite for all parser tasks]`

- [ ] Extend `tests/fixtures/pjud/detail_civil_rich.html`
  - Add form for `docuS.php?dtaDoc=<fake_jwt>` (resolution movement)
  - Add form for `docCertificadoEscrito.php?dtaCert=FAKE_JWT_A` (escrito_cert, disambiguation A)
  - Add form for `docu.php?valorEncTxtDmda=<fake_jwt>` (texto_demanda, case-level)
  - Add form for `docCertificadoDemanda.php?dtaCert=FAKE_JWT_B` (cert_envio, disambiguation B)
  - Add form for `newebookcivil.php?dtaEbook=<fake_jwt>` (ebook, case-level)
- [ ] Extend `tests/fixtures/pjud/detail_civil_synthetic.html`
  - Add `<i class="fas fa-ban">` icon in the cert_envio slot (fa-ban scenario)
  - Ensure cert_envio form is absent — only the icon
- [ ] Use only synthetic fake JWTs (`eyJhbGciOiJub25lIn0.e30.FAKE`) — no real tokens, no PII
- **Spec**: Static Document Token Extraction (all four scenarios)
- **Commit**: `test(fixtures): extend civil HTML fixtures with document form tokens`

---

### S1-T2 — Write failing parser tests `[PARALLEL with S1-T3, S1-T6, S1-T8, S1-T9 — after S1-T1]`

- [ ] In `tests/scrapper/pjud/test_civil_parsers.py`, add test class `TestStaticDocumentTokenExtraction`
- [ ] `test_all_five_static_token_types_parsed`: use `detail_civil_rich.html` fixture; assert `PJUDCaseDetail.texto_demanda_token`, `cert_envio_token`, `ebook_token` all non-null; assert `movements[0].document_token` non-null
- [ ] `test_dtaCert_disambiguation_by_endpoint`: assert `escrito_cert` token == FAKE_JWT_A, `cert_envio` token == FAKE_JWT_B
- [ ] `test_fa_ban_yields_none_without_exception`: use synthetic fixture; assert `cert_envio_token is None`; assert no exception raised
- [ ] `test_missing_form_yields_none_without_exception`: use a fixture with neither form nor fa-ban for cert_envio slot; assert `cert_envio_token is None`
- [ ] **Tests MUST fail** at this point — implementation not yet done
- **Spec**: Static Document Token Extraction (scenarios 1–4)
- **Commit**: `test(scrapper): failing tests for 5-token extraction and dtaCert disambiguation`

---

### S1-T3 — Extend PJUDDocument + PJUDCaseDetail dataclasses `[PARALLEL with S1-T2, S1-T6, S1-T8, S1-T9]`

- [ ] In `app/scrapper/pjud/base.py`, extend `PJUDDocument`:
  - Add `doc_type: Optional[str] = None`
  - Add `endpoint: Optional[str] = None`
  - Add `param_name: Optional[str] = None`
  - Add `available: bool = True`
- [ ] In `app/scrapper/pjud/base.py`, extend `PJUDCaseDetail`:
  - Add `case_documents: List[PJUDDocument] = field(default_factory=list)`
- [ ] All new fields use defaults — existing callers are backward-compatible
- **Spec**: Static Document Token Extraction; Design ADR-2
- **Commit**: `feat(pjud): extend PJUDDocument + PJUDCaseDetail with doc-type carrier fields`

---

### S1-T4 — Update civil.yaml selectors `[SEQUENTIAL — after S1-T3]`

- [ ] In `app/scrapper/pjud/selectors/civil.yaml`, add selectors:
  - `escrito_cert_form_action`: matches `form[action*="docCertificadoEscrito.php"]`
  - `escrito_cert_param`: input `name="dtaCert"` inside that form
  - `texto_demanda_form_action`: matches `form[action*="docu.php"]`
  - `texto_demanda_param`: input `name="valorEncTxtDmda"`
  - `cert_envio_form_action`: matches `form[action*="docCertificadoDemanda.php"]`
  - `cert_envio_param`: input `name="dtaCert"`
  - `ebook_form_action`: matches `form[action*="newebookcivil.php"]`
  - `ebook_param`: input `name="dtaEbook"`
  - `disabled_doc_indicator`: `i.fas.fa-ban` (presence = doc unavailable)
- [ ] Add YAML comment documenting the dtaCert disambiguation rule
- **Spec**: Static Document Token Extraction; Design: Token map
- **Commit**: `feat(selectors): add document form selectors for 5 static token types`

---

### S1-T5 — Implement parser in civil.py `[SEQUENTIAL — after S1-T2 + S1-T3 + S1-T4]`

- [ ] In `app/scrapper/pjud/civil.py`, update `_parse_case_detail_html` (or equivalent):
  - Extract movement-level tokens (`docuS.php` / `docuN.php`) into `PJUDMovement.documentos` — **this fixes the existing bug where tokens were never persisted**
  - Extract `escrito_cert` token anchored on form action `docCertificadoEscrito.php`
  - Extract case-level `texto_demanda`, `cert_envio`, `ebook` tokens via respective form actions
  - Detect `fa-ban` icon in each slot → `available=False`, token value stays as empty string or `None`
  - Missing form (and no fa-ban) → token=`None`, no exception raised
  - Populate `PJUDCaseDetail.case_documents` list with the 3 case-level `PJUDDocument` instances
- [ ] All S1-T2 parser tests MUST pass after this step
- **Spec**: Static Document Token Extraction (all four scenarios)
- **Commit**: `feat(scrapper): parse 5 static document tokens, dtaCert disambiguation, fa-ban handling`

---

### S1-T6 — Write failing identity hash tests `[PARALLEL with S1-T2, S1-T3, S1-T8, S1-T9]`

- [ ] Create `tests/services/test_document_persistence.py`
- [ ] `test_same_identity_returns_same_hash`: call `document_identity_hash` twice with same args but different `jwt_value` (not a param) → same result
- [ ] `test_different_scope_key_returns_different_hash`: same doc_type + case_rol, different scope_key → different hash
- [ ] `test_empty_scope_key_valid_for_case_level_docs`: `scope_key=""` → produces valid hex string, no exception
- [ ] `test_hash_is_64_char_hex`: result is exactly 64 hex characters (SHA-256)
- [ ] **Tests MUST fail** — implementation not yet done
- **Spec**: Token Persistence Idempotency; Design ADR-1
- **Commit**: `test(persistence): failing tests for stable document identity hash (ADR-1)`

---

### S1-T7 — Implement document_identity_hash `[SEQUENTIAL — after S1-T6]`

- [ ] Create `app/services/document_persistence.py`
- [ ] Implement `def document_identity_hash(doc_type: str, case_rol: str, scope_key: str = "") -> str`
  - `hashlib.sha256(f"{doc_type}|{case_rol}|{scope_key}".encode()).hexdigest()`
- [ ] All S1-T6 tests MUST pass
- **Spec**: Token Persistence Idempotency; Design ADR-1
- **Commit**: `feat(persistence): implement document_identity_hash — stable business identity, not JWT hash`

---

### S1-T8 — Modify Document model `[PARALLEL with S1-T2, S1-T3, S1-T6, S1-T9]`

- [ ] In `app/models/document.py`, add columns to the `Document` class:
  - `doc_type = Column(String(50), nullable=True)` — nullable for migration compat, functionally required
  - `pjud_endpoint = Column(String(255), nullable=True)` — e.g., `"docuS.php"`
  - `pjud_token = Column(Text, nullable=True)` — live JWT, refreshed each sync
  - `pjud_token_hash = Column(String(64), nullable=True, unique=True)` — stable identity SHA-256
  - `status = Column(String(20), nullable=False, server_default="pending")` — `pending|stored|failed|unavailable`
  - `escrito_id = Column(Integer, ForeignKey("case_escritos.id"), nullable=True)`
- [ ] Add relationship: `escrito = relationship("CaseEscrito", backref="documents")`
- [ ] Keep existing columns intact (`gcs_path`, `movement_id`, `filename`, `content_type`, etc.)
- [ ] Make `filename` nullable in the model (download has not happened yet at persist time): `filename = Column(String(255), nullable=True)`
- **Spec**: Document Token Model; Design ADR-3
- **Commit**: `feat(models): add doc_type, pjud_token, pjud_token_hash, status, escrito_id to Document`

---

### S1-T9 — Add Movement.document_token `[PARALLEL with S1-T2, S1-T3, S1-T6, S1-T8]`

- [ ] In `app/models/movement.py`, add:
  - `document_token = Column(Text, nullable=True)` — denormalized copy of primary doc JWT (bug fix)
- **Spec**: Document Token Model ("Movement MUST gain a document_token column"); Design ADR-3
- **Commit**: `fix(models): add document_token to Movement — token was silently dropped on sync`

---

### S1-T10 — Create Alembic migration 006 `[SEQUENTIAL — after S1-T8 + S1-T9]`

- [ ] Create `alembic/versions/006_add_document_tokens.py`
- [ ] `upgrade()`:
  - `ALTER TABLE documents ADD COLUMN doc_type VARCHAR(50)`
  - `ALTER TABLE documents ADD COLUMN pjud_endpoint VARCHAR(255)`
  - `ALTER TABLE documents ADD COLUMN pjud_token TEXT`
  - `ALTER TABLE documents ADD COLUMN pjud_token_hash VARCHAR(64)`
  - `CREATE UNIQUE INDEX ix_documents_pjud_token_hash ON documents (pjud_token_hash)` (partial: WHERE pjud_token_hash IS NOT NULL)
  - `ALTER TABLE documents ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'pending'`
  - `ALTER TABLE documents ADD COLUMN escrito_id INTEGER REFERENCES case_escritos(id)`
  - `ALTER TABLE documents ALTER COLUMN filename DROP NOT NULL` (make nullable)
  - `ALTER TABLE movements ADD COLUMN document_token TEXT`
- [ ] `downgrade()`: drop all added columns and the unique index in reverse order
- [ ] All columns additive and nullable — no data migration required
- **Spec**: Document Token Model; Design: Migration/Rollout
- **Commit**: `feat(alembic): migration 006 — document token columns and movement.document_token`

---

### S1-T11 — Write failing DocumentPersistenceService tests `[SEQUENTIAL — after S1-T7 + S1-T8 + S1-T10]`

- [ ] In `tests/services/test_document_persistence.py` (extend file from S1-T6), add `TestDocumentPersistenceService`
- [ ] `test_persist_creates_document_row_with_correct_doc_type`: mock detail with one resolution movement; assert `Document` created with `doc_type="resolution"`, non-null `pjud_token_hash`, `status="pending"`
- [ ] `test_persist_sets_movement_document_token`: assert `Movement.document_token` set after persist
- [ ] `test_re_sync_does_not_duplicate_rows`: persist same detail twice; assert DB count for case remains 5 (not 10)
- [ ] `test_fa_ban_persists_as_unavailable`: detail has fa-ban cert_envio; assert `Document.status="unavailable"`, `pjud_token=None`
- [ ] `test_unavailable_document_never_downloaded`: mock a downloader; assert downloader not called for unavailable doc
- [ ] Use SQLite in-memory with SQLAlchemy (or the existing `db_session` conftest fixture)
- [ ] **Tests MUST fail** — implementation not yet done
- **Spec**: Document Token Model, Token Persistence Idempotency (all scenarios)
- **Commit**: `test(persistence): failing tests for DocumentPersistenceService upsert and idempotency`

---

### S1-T12 — Implement DocumentPersistenceService `[SEQUENTIAL — after S1-T11]`

- [ ] In `app/services/document_persistence.py` (extend from S1-T7), add `DocumentPersistenceService`:
  - `def persist_from_detail(self, detail: PJUDCaseDetail, case_id: int, db: Session) -> List[Document]`
  - Iterate `detail.case_documents` (case-level) + all `movement.documentos` (movement-level)
  - For each doc: compute `pjud_token_hash = document_identity_hash(doc.doc_type, case.rol, scope_key)`
    - `scope_key` = movement folio for movement-level, `""` for case-level
  - `SELECT Document WHERE pjud_token_hash = X`
    - If found and `status="stored"`: skip (idempotent)
    - If found and `status in ("pending", "failed")`: update `pjud_token` (JWT refreshed)
    - If not found: `INSERT Document(case_id, doc_type, pjud_endpoint, pjud_token, pjud_token_hash, status, movement_id)`
      - `status="unavailable"` when `doc.available is False`, else `"pending"`
  - For movement-level docs: also set `movement.document_token = doc.token`
  - Return list of inserted/updated Document objects
- [ ] All S1-T11 tests MUST pass
- **Spec**: Document Token Model, Token Persistence Idempotency
- **Commit**: `feat(persistence): DocumentPersistenceService — upsert by stable hash, fa-ban→unavailable`

---

### S1-T13 — Wire persistence into sync_service `[SEQUENTIAL — after S1-T12]`

- [ ] In `app/services/sync_service.py`, after the existing entity persistence (litigantes, notificaciones, etc.):
  - Instantiate `DocumentPersistenceService`
  - Call `persist_from_detail(detail, case_id, db)`
  - `DOC_DOWNLOAD_ENABLED` defaults `False` at this stage — no downloads yet
- [ ] Existing sync tests in `tests/services/test_case_detail_sync.py` and `tests/test_sync_service.py` MUST still pass
- **Spec**: "Movement document token persisted after sync" (end-to-end scenario)
- **Commit**: `feat(sync): wire DocumentPersistenceService into sync flow (tokens only, no download yet)`

---

### S1-T14 — Slice 1 verification gate `[SEQUENTIAL — last in slice]`

- [ ] `pytest -m "not integration"` → all pass (new tests + existing regression)
- [ ] `mypy app/core` → no type errors
- [ ] `alembic upgrade head` → exits 0
- [ ] `alembic downgrade -1` → exits 0 (confirms rollback path works)
- [ ] `alembic upgrade head` → exits 0 (confirms re-apply is clean)
- [ ] Grep fixtures for real JWT patterns (`eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`) — MUST contain only synthetic tokens
- [ ] Open PR #1 (`feat/case-documents-slice-1 → main`) — size:exception accepted (see Review Workload Forecast)

---

## Slice 2 — StorageService + Synchronous Download + Documents API

**Depends on**: Slice 1 merged to main
**PR branch**: `feat/case-documents-slice-2 → main` (rebased on merged Slice 1)
**Rollback boundary**: set `DOC_DOWNLOAD_ENABLED=false` in env; no DB migration to reverse; GCS uploads idempotent
**Verification gate**: all checks in S2-T10 must be green before opening PR #2

### S2-T1 — Add config fields `[SEQUENTIAL first — blocks factory in tests]`

- [ ] In `app/config.py` (`Settings` class), add:
  - `GCS_BUCKET: str = ""`  — empty string → LocalStorageBackend; set → GCSStorageBackend
  - `GCS_SIGNED_URL_TTL: int = 3600`
  - `DOC_DOWNLOAD_ENABLED: bool = False`
- **Spec**: GCS Storage — Idempotent Upload; Design ADR-4
- **Commit**: `feat(config): add GCS_BUCKET, GCS_SIGNED_URL_TTL, DOC_DOWNLOAD_ENABLED`

---

### S2-T2 — Write failing StorageService tests `[PARALLEL with S2-T4, S2-T7 — after S2-T1]`

- [ ] Create `tests/services/test_storage_service.py`
- [ ] `test_local_backend_upload_writes_bytes(tmp_path)`: upload bytes → file exists on disk; returned uri non-null
- [ ] `test_local_backend_exists_true_after_upload(tmp_path)`: exists() returns False before, True after upload
- [ ] `test_local_backend_signed_url_returns_string(tmp_path)`: signed_url() returns a non-empty string
- [ ] `test_storage_service_skips_upload_if_already_stored`: create Document with `gcs_path` set; mock backend.exists → True; assert backend.upload NOT called
- [ ] `test_storage_service_uploads_and_sets_gcs_path`: Document with `gcs_path=None`; mock backend.exists → False; mock backend.upload → "gs://bucket/key"; assert Document.gcs_path set + status=stored
- [ ] `test_factory_returns_local_when_no_bucket(monkeypatch)`: GCS_BUCKET="" → `LocalStorageBackend`
- [ ] `test_factory_returns_gcs_when_bucket_set(monkeypatch)`: GCS_BUCKET="my-bucket" → `GCSStorageBackend`
- [ ] **Tests MUST fail**
- **Spec**: GCS Storage — Idempotent Upload; Design ADR-4
- **Commit**: `test(storage): failing tests for StorageBackend, LocalStorageBackend, and idempotency`

---

### S2-T3 — Implement storage_service.py `[SEQUENTIAL — after S2-T1 + S2-T2]`

- [ ] Create `app/services/storage_service.py`
- [ ] `class StorageBackend(Protocol)`:
  - `def upload(self, data: bytes, key: str, content_type: str) -> str: ...`
  - `def signed_url(self, key: str, expires_s: int) -> str: ...`
  - `def exists(self, key: str) -> bool: ...`
- [ ] `class LocalStorageBackend(StorageBackend)`:
  - Stores files at `{base_dir}/{key}`
  - `signed_url` returns `f"file://{abs_path}"` (dev/test only)
  - Key layout: `cases/{case_id}/{doc_type}/{pjud_token_hash}.pdf`
- [ ] `class GCSStorageBackend(StorageBackend)`:
  - Uses `google.cloud.storage.Client()` (ADC)
  - `upload` → `blob.upload_from_string`; returns `f"gs://{bucket}/{key}"`
  - `signed_url` → `blob.generate_signed_url(expiration=timedelta(seconds=expires_s))`
  - `exists` → `blob.exists()`
- [ ] `class StorageService`:
  - `__init__(self, backend: StorageBackend)`
  - `def upload(self, doc: Document, data: bytes, content_type: str = "application/pdf") -> None`
    - key = `document_storage_key(doc)` helper
    - if `backend.exists(key)` → skip (idempotent)
    - else: uri = `backend.upload(data, key, content_type)`; doc.gcs_path = uri; doc.status = "stored"
  - `def signed_url(self, doc: Document, ttl: int) -> Optional[str]`
    - returns None if `doc.gcs_path` is None
    - else `backend.signed_url(key, ttl)`
- [ ] `def make_storage_backend(settings) -> StorageBackend`: factory
- [ ] All S2-T2 tests MUST pass
- **Spec**: GCS Storage — Idempotent Upload; Design ADR-4
- **Commit**: `feat(storage): StorageBackend protocol, LocalStorageBackend, GCSStorageBackend, StorageService`

---

### S2-T4 — Write failing DocumentDownloader tests `[PARALLEL with S2-T2, S2-T7 — after S2-T1]`

- [ ] Create `tests/services/test_document_downloader.py`
- [ ] `test_failure_isolation_second_doc_fails`:
  - 3 pending Document fixtures; mock `page.evaluate`: first returns bytes, second raises `httpx.RequestError`, third returns bytes
  - run downloader; assert doc 1 + 3 `status="stored"`, doc 2 `status="failed"`; assert no exception propagated
- [ ] `test_rate_limiter_called_between_downloads`:
  - 2 pending docs; inject mock `rate_limiter`; run; assert `rate_limiter.wait()` (or equivalent) called >= 1 time
- [ ] `test_doc_download_enabled_false_skips_all`:
  - 2 pending docs; `DOC_DOWNLOAD_ENABLED=False`; run; assert zero fetch calls
- [ ] `test_unavailable_docs_skipped`:
  - 1 unavailable doc; run with `DOC_DOWNLOAD_ENABLED=True`; assert zero fetch calls
- [ ] **Tests MUST fail**
- **Spec**: Download Failure Isolation, Download Rate Limiting, Synchronous Download Within Token TTL
- **Commit**: `test(downloader): failing tests for failure isolation, throttle, and DOC_DOWNLOAD_ENABLED gate`

---

### S2-T5 — Implement document_downloader.py `[SEQUENTIAL — after S2-T3 + S2-T4]`

- [ ] Create `app/services/document_downloader.py`
- [ ] `class DocDownloadStrategy(Protocol)`:
  - `async def fetch(self, page, doc: Document) -> bytes: ...` — seam for Slice 3 AjaxStrategy
- [ ] `class StaticFormStrategy(DocDownloadStrategy)`:
  - `async def fetch(self, page, doc: Document) -> bytes`
  - Calls `page.evaluate(js_fetch, {endpoint: doc.pjud_endpoint, param: doc.pjud_token})` — reuses live browser cookies
- [ ] `class DocumentDownloader`:
  - `async def run(self, case_id: int, pending_docs: List[Document], page, db: Session, storage_service: StorageService, limiter, enabled: bool) -> None`
  - If `not enabled`: return immediately
  - For each doc where `doc.status == "pending"`:
    - `await limiter.wait()` (injects delay >= 1s; limiter is injectable for tests)
    - `try: data = await strategy.fetch(page, doc); storage_service.upload(doc, data); db.commit()`
    - `except Exception: doc.status = "failed"; db.commit()`
  - Skip docs with `status != "pending"` (unavailable, stored, failed)
- [ ] All S2-T4 tests MUST pass
- **Spec**: Download Failure Isolation, Download Rate Limiting, Synchronous Download Within Token TTL; Design ADR-5
- **Commit**: `feat(downloader): DocumentDownloader, StaticFormStrategy, failure isolation, rate-limit gate`

---

### S2-T6 — Wire downloader into sync_service `[SEQUENTIAL — after S2-T5]`

- [ ] In `app/services/sync_service.py`, after `DocumentPersistenceService.persist_from_detail`:
  - Build `pending_docs = [d for d in persisted_docs if d.status == "pending"]`
  - Instantiate `StorageService(make_storage_backend(settings))`
  - Await `DocumentDownloader().run(case_id, pending_docs, live_page, db, storage_service, limiter, settings.DOC_DOWNLOAD_ENABLED)`
- [ ] `DOC_DOWNLOAD_ENABLED` defaults `False` in `.env` and tests — existing tests MUST still pass
- **Spec**: Synchronous Download Within Token TTL; Design ADR-5
- **Commit**: `feat(sync): wire DocumentDownloader into sync flow, gated by DOC_DOWNLOAD_ENABLED`

---

### S2-T7 — Write failing Documents API tests `[PARALLEL with S2-T2, S2-T4 — after S2-T1]`

- [ ] Create `tests/api/v1/test_documents_api.py`
- [ ] `test_list_documents_returns_all_for_case`:
  - Seed case 42 with 2 stored + 1 unavailable Document rows; GET `/cases/42/documents`; assert 3 items; stored items have non-null `signed_url`; unavailable item has `signed_url=null`
- [ ] `test_list_documents_empty_case_returns_empty_list`:
  - Case with no documents; GET `/cases/99/documents`; assert `[]`
- [ ] `test_get_document_stored_redirects`:
  - Document 7, status=stored, gcs_path set; mock `StorageService.signed_url` → `"https://storage.example.com/file"`; GET `/documents/7`; assert `status_code in (302, 307)` and `Location` header set
- [ ] `test_get_document_unknown_returns_404`:
  - GET `/documents/999`; assert 404
- [ ] `test_get_document_not_stored_returns_404`:
  - Document status=pending or failed; GET `/documents/{id}`; assert 404
- [ ] **Tests MUST fail**
- **Spec**: Documents API (all three scenarios)
- **Commit**: `test(api): failing tests for GET /cases/{id}/documents and GET /documents/{id}`

---

### S2-T8 — Implement documents.py API `[SEQUENTIAL — after S2-T3 + S2-T7]`

- [ ] Create `app/api/v1/documents.py`
- [ ] Schema: `DocumentResponse(id: int, doc_type: str, status: str, filename: Optional[str], size_bytes: Optional[int], document_date: Optional[datetime], signed_url: Optional[str])`
- [ ] `GET /cases/{case_id}/documents`:
  - Query `Document` where `case_id = case_id`
  - For each: `signed_url = storage_service.signed_url(doc, settings.GCS_SIGNED_URL_TTL) if doc.status == "stored" else None`
  - Return `List[DocumentResponse]`
- [ ] `GET /documents/{doc_id}`:
  - Fetch Document; 404 if not found
  - 404 if `status != "stored"`
  - Return `RedirectResponse(url=storage_service.signed_url(doc, ttl), status_code=307)`
- [ ] Inject `db: Session = Depends(get_db)` and `StorageService` via `Depends`
- [ ] All S2-T7 tests MUST pass
- **Spec**: Documents API; Design: Interfaces section
- **Commit**: `feat(api): GET /cases/{id}/documents and GET /documents/{id} with signed-URL redirect`

---

### S2-T9 — Register documents router `[SEQUENTIAL — after S2-T8]`

- [ ] In `app/api/v1/router.py`:
  - Import `from app.api.v1 import documents`
  - `api_router.include_router(documents.router, prefix="", tags=["documents"])`
  - (The documents router itself owns the `/cases/{id}/documents` and `/documents/{id}` prefixes)
- **Spec**: Documents API
- **Commit**: `feat(api): register documents routes in v1 router`

---

### S2-T10 — Slice 2 verification gate `[SEQUENTIAL — last in slice]`

- [ ] `pytest -m "not integration"` → all pass
- [ ] `mypy app/core` → no type errors
- [ ] `alembic upgrade head` / `alembic downgrade -1` / `alembic upgrade head` → all exits 0
- [ ] Smoke test: start app with `DOC_DOWNLOAD_ENABLED=false`; call `GET /cases/1/documents` → 200 (empty or populated)
- [ ] Confirm `google-cloud-storage` import is conditional (GCSStorageBackend only instantiated when `GCS_BUCKET` is set) so tests without GCP creds do not fail
- [ ] Open PR #2 (`feat/case-documents-slice-2 → main`) — size:exception accepted (see Review Workload Forecast)

---

## Slice 3 — AJAX Documents (BLOCKED — do NOT implement)

**Status**: BLOCKED
**Reason**: Endpoints for AJAX document types (`anexoCausaCivil`, `receptorCivil`, `receptorCivilReserva`) are unknown. Live network trace required to capture request URLs, parameters, and response format.
**Seam already reserved**:
- `doc_type` enum space reserves `anexo_causa`, `receptor`, `receptor_reserva`
- `DocDownloadStrategy` protocol accommodates `AjaxStrategy` without core changes
- `Document.pjud_endpoint` is nullable → AJAX docs can persist as `status=unavailable` until traced
- New AJAX type = new parser entry + `AjaxStrategy` implementation only

**No tasks assigned to Slice 3.**

---

## Review Workload Forecast

| Metric | Slice 1 | Slice 2 | Total |
|---|---|---|---|
| Estimated production lines | ~375 | ~360 | ~735 |
| Estimated test + fixture lines | ~330 | ~320 | ~650 |
| **Total estimated changed lines** | **~705** | **~680** | **~1 385** |
| 400-line budget risk | **HIGH** | **HIGH** | **HIGH** |
| Chained PRs recommended | **YES** — Slice 1 = PR #1 | **YES** — Slice 2 = PR #2 | stacked-to-main |
| Decision needed before apply | **NO** — each slice ships independently; `size:exception` accepted per slice | | |

**Rationale for size:exception per slice**: Slices 1 and 2 are already the minimum coherent deliverables. Slice 1 produces tokens in the DB with zero download plumbing. Slice 2 completes the download pipeline. Splitting within a slice would break "tests ship with code" and produce non-deployable intermediates. Maintainer acceptance of `size:exception` (one per PR) is the right call here.

---

## Task Dependency Summary

```
Slice 1:
S1-T1 ──→ S1-T2 ──┐
        → S1-T3 ──┼──→ S1-T4 ──→ S1-T5 (parser impl, all S1-T2 tests pass)
        → S1-T6 ──→ S1-T7 ──────────────────────────────┐
        → S1-T8 ──┬──→ S1-T10 ──→ S1-T11 ──→ S1-T12 ──→ S1-T13 ──→ S1-T14
        → S1-T9 ──┘

Slice 2 (after Slice 1 merged):
S2-T1 ──→ S2-T2 ──→ S2-T3 ──┬──→ S2-T5 ──→ S2-T6 ──→ S2-T10
        → S2-T4 ──────────────┘
        → S2-T7 ──→ S2-T8 ──→ S2-T9 ──┘
```

**Parallel groups**:
- Slice 1: `{S1-T2, S1-T3, S1-T6, S1-T8, S1-T9}` can run in parallel after S1-T1
- Slice 2: `{S2-T2, S2-T4, S2-T7}` can run in parallel after S2-T1; S2-T1 itself unblocks immediately
