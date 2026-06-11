# Delta for pjud-civil

## ADDED Requirements

### Requirement: Static Document Token Extraction

`_parse_case_detail_html` MUST extract all static document tokens from the case detail HTML
and populate them on `PJUDCaseDetail` (case-level) and each `PJUDMovement` (movement-level).
`dtaCert` appears as a param name in two different forms; they MUST be disambiguated by form
action endpoint, not by param name. A document slot rendered as `<i class="fas fa-ban">` MUST
yield `token=None` for that type; the parser MUST NOT raise an exception.

| doc_type | form action | param |
|---|---|---|
| `resolution` | `docuS.php` | `dtaDoc` |
| `escrito_doc` | `docuN.php` | `dtaDoc` |
| `escrito_cert` | `docCertificadoEscrito.php` | `dtaCert` |
| `texto_demanda` | `docu.php` | `valorEncTxtDmda` |
| `cert_envio` | `docCertificadoDemanda.php` | `dtaCert` |
| `ebook` | `newebookcivil.php` | `dtaEbook` |

#### Scenario: All five static token types parsed from detail HTML

- GIVEN the C-7762-2026 detail HTML with 2 movement rows, texto demanda, cert envío, and ebook forms
- WHEN `_parse_case_detail_html` runs
- THEN `PJUDCaseDetail.texto_demanda_token`, `cert_envio_token`, and `ebook_token` are all non-null
- AND `movements[0].document_token` and `movements[1].document_token` are both non-null

#### Scenario: dtaCert disambiguation by endpoint

- GIVEN a detail HTML containing both `docCertificadoEscrito.php?dtaCert=JWT_A` and `docCertificadoDemanda.php?dtaCert=JWT_B`
- WHEN parsing runs
- THEN `escrito_cert` token MUST equal `JWT_A`
- AND `cert_envio` token MUST equal `JWT_B`

#### Scenario: fa-ban yields None token without error

- GIVEN the C-1253-2015 detail HTML where `cert_envio` and `anexos` render as `<i class="fas fa-ban">`
- WHEN `_parse_case_detail_html` runs
- THEN `PJUDCaseDetail.cert_envio_token` MUST be `None`
- AND no exception is raised

#### Scenario: Missing form for optional token yields None

- GIVEN a detail HTML that has neither a `docCertificadoDemanda.php` form nor an `fa-ban` icon for that slot
- WHEN `_parse_case_detail_html` runs
- THEN `PJUDCaseDetail.cert_envio_token` MUST be `None`
- AND no exception is raised
