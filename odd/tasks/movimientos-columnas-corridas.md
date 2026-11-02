# Movements parser: stop writing corrupted duplicate movements
The PJUD movements table is read by FIXED cell position; when the page has one cell fewer, every column shifts and the sync stores corrupted duplicates.

## Specs
- S1. Evidence (production, 2026-11-02): rows with `description` = a date (dd/mm/yyyy) and `movement_date` = insert time. Example case 16918: original row stage='Inicio de la Tramitación', procedure='Escrito', description='Ingreso demanda', folio='1', movement_date=2025-10-30; corrupted row: same folio, description='30/10/2025', movement_date=now. 10,592 rows / 826 cases since 2026-10-26.
- S2. Root cause (read from code): `_parse_movements_table` in app/scrapper/pjud/civil.py (~lines 610-710) splits the row with `re.split(r'</td>', ...)` and reads `descripcion=cells[6]`, fecha from `cells[7]`, foja `cells[8]`, while the comment says 5=Desc, 6=Fecha, 7=Foja. The fixture tests/fixtures/pjud/detail_civil_rich.html (~line 178) has an HTML-commented `<td>` (`<!--<td><span title="Exhórtese" ...></span></td>-->`) whose `</td>` adds a phantom cell; when the page omits it, columns shift by one. laboral.py (~376) and penal.py (~386) use the same indexes.
- S3. Fix the parsers (civil first; laboral and penal the same way if their code has the same pattern): strip HTML comments from the row BEFORE splitting into cells (`re.sub(r'<!--.*?-->', '', row, flags=re.S)`), and locate the movement date by SHAPE: the first cell after the Trámite cell that matches `^\d{2}/\d{2}/\d{4}$`; `descripcion` is the cell immediately BEFORE the date cell and `foja` the one immediately AFTER it. The result must be identical whether or not the commented cell is present. Keep Folio/Doc/Etapa/Trámite parsing as is.
- S4. Persistence (app/services/sync_service.py, `_upsert_movement`, ~1565-1600): a movement whose date does not parse must NOT be stored with `datetime.utcnow()`; skip it and log a WARNING with case id and folio (counted so the cycle summary can show it if the code already has a counter for skipped rows; do not invent new infrastructure). Also skip a row whose description is only a date (dd/mm/yyyy) with the same log. Do NOT change the dedup key (folio, description) in this task.
- S5. Idempotency: re-syncing the same table twice inserts nothing the second time (a test).
- S6. Tests first (strict TDD, show RED): parser with the commented cell present and absent (same description/date/foja), shifted-layout row, laboral and penal if applicable, invalid/empty date -> skipped and never `now`, description-only-date -> skipped, idempotent re-sync. Existing tests must keep passing (fixtures under tests/fixtures/pjud).
- S7. Out of scope: any production data cleanup or DELETE, any change to deadlines/semaforo logic, the dedup key, the worker or deployment.

## Tasks
- T1 (S1-S7) route: worker. Commit: none (the parent reviews, then commits).

## Log
L1. "si dale" to implementing the parser fix after the investigation (corrupted duplicate movements, 10,592 rows in 826 cases).
