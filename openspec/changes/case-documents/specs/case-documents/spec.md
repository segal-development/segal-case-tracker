# case-documents Specification

## Purpose

Parse, persist, download, and serve PJUD civil case PDFs from static-HTML endpoints.
Scope: Slice 1 (token capture + DB + migration) and Slice 2 (GCS + synchronous download + API).
AJAX document types (anexos, receptor) are out of scope pending live network trace (Slice 3).

## Requirements

### Requirement: Document Token Model

The system MUST store one `Document` row per (case, doc_type, movement_id/escrito_id).
Required columns: `doc_type`, `endpoint`, `pjud_token`, `pjud_token_hash` (SHA-256 of token),
`status` (`pending` | `stored` | `failed` | `unavailable`), `gcs_path` (nullable), `escrito_id`
(nullable FK). `Movement` MUST gain a `document_token` column (nullable string).
A document rendered as `fa-ban` in the source HTML MUST yield `pjud_token=None`
and `status=unavailable`; no download attempt is ever made for it.

| doc_type | endpoint | param |
|---|---|---|
| `resolution` | `docuS.php` | `dtaDoc` |
| `escrito_doc` | `docuN.php` | `dtaDoc` |
| `escrito_cert` | `docCertificadoEscrito.php` | `dtaCert` |
| `texto_demanda` | `docu.php` | `valorEncTxtDmda` |
| `cert_envio` | `docCertificadoDemanda.php` | `dtaCert` |
| `ebook` | `newebookcivil.php` | `dtaEbook` |

#### Scenario: Movement document token persisted after sync

- GIVEN a sync for C-7762-2026 where folio 2 has a `docuS.php` form
- WHEN the sync completes
- THEN a `Document` row with `doc_type=resolution` and a non-null `pjud_token_hash` MUST exist
- AND `Movement.document_token` for that folio MUST be non-null

#### Scenario: fa-ban document stored as unavailable without download

- GIVEN the C-1253-2015 detail HTML where `cert_envio` renders as `<i class="fas fa-ban">`
- WHEN the sync completes
- THEN a `Document` row with `doc_type=cert_envio`, `pjud_token=None`, `status=unavailable` MUST exist
- AND no download call is made for that row

### Requirement: Token Persistence Idempotency

The system MUST upsert `Document` rows keyed on `pjud_token_hash`. Re-syncing the same
detail HTML MUST NOT create additional rows for the same case.

#### Scenario: Re-sync does not duplicate Document rows

- GIVEN a case already synced with 5 Document rows
- WHEN the identical detail HTML is synced again
- THEN the Document count for that case remains exactly 5

### Requirement: GCS Storage — Idempotent Upload

The system MUST skip download and GCS upload when a `Document` row for the given
`pjud_token_hash` already has a non-null `gcs_path`. New rows MUST be uploaded once;
`gcs_path` MUST be set on success.

#### Scenario: Already-uploaded document skipped on re-sync

- GIVEN a `Document` row with `pjud_token_hash=abc123` and `gcs_path` set
- WHEN a new sync encounters the same hash
- THEN no download request is made
- AND no GCS upload call is made

### Requirement: Synchronous Download Within Token TTL

The system MUST initiate all static-endpoint downloads in the same sync task, within
3600 seconds of token `iat`, reusing the active browser session (authenticated cookies).
No download MUST be queued or deferred to a background job.

#### Scenario: Download initiated inside the 1-hour window

- GIVEN a sync with mocked clock at T0 = token `iat`
- WHEN all static downloads are triggered
- THEN each download call timestamp MUST be < T0 + 3600s

### Requirement: Download Failure Isolation

A failed download (network error or HTTP status >= 400) MUST set `status=failed` on
the `Document` row. The sync MUST NOT raise an exception. All other documents in the
same sync MUST continue processing independently.

#### Scenario: Single download failure does not halt sync

- GIVEN a case with 3 static documents where the second download raises `httpx.RequestError`
- WHEN the sync runs
- THEN documents 1 and 3 have `status=stored` and documents 2 has `status=failed`
- AND the sync task completes without raising an exception

### Requirement: Download Rate Limiting

The system MUST apply at least 1 second of delay between consecutive document download
requests within a single sync run. The rate-limiter MUST be injectable in tests.

#### Scenario: Delay applied between consecutive downloads

- GIVEN a case with 2 static documents and an injected mock rate limiter
- WHEN both downloads execute
- THEN the rate limiter is called at least once with a delay value >= 1 second

### Requirement: Documents API

`GET /cases/{case_id}/documents` MUST return all `Document` rows for that case with
`doc_type`, `status`, and `signed_url` (non-null only when `status=stored`).
`GET /documents/{doc_id}` MUST redirect (302/307) to the GCS signed URL when
`status=stored`, or return 404 if the document does not exist or has any other status.

#### Scenario: List returns all document types with correct fields

- GIVEN case 42 with 3 Document rows (2 `stored`, 1 `unavailable`)
- WHEN `GET /cases/42/documents` is called
- THEN 3 items are returned; stored items include a non-null `signed_url`; the unavailable item has `signed_url=null`

#### Scenario: Download endpoint redirects to signed URL

- GIVEN document 7 with `status=stored` and a valid `gcs_path`
- WHEN `GET /documents/7` is called
- THEN the response is a 302 or 307 redirect to the GCS signed URL

#### Scenario: Download endpoint returns 404 for unknown document

- GIVEN no Document row exists with `id=999`
- WHEN `GET /documents/999` is called
- THEN the response status MUST be 404
