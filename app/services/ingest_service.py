"""IngestService — bulk-persists PJUD cases relayed by the browser extension.

Adapted from ``scripts/import_cases_html.py``'s fast bulk-insert path
(preload existing rols + ``SyncService._get_or_create_court`` +
``bulk_insert_mappings``) rather than the per-case ``SyncService.sync_cases``,
which was too slow through the operator's proxy (see design ADR in
sdd/conectar-pjud-extension/design).
"""

import base64
import binascii
import re
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Integer, cast, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.auth import _get_or_create_lawyer
from app.models.case import Case
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.scrapper.pjud.civil import CivilScraper
from app.services.document_persistence import DocumentPersistenceService
from app.services.sync_service import (
    SyncService,
    _maybe_recompute_deadlines,
    convert_api_movements_to_scraped,
)
from app.utils.rut import normalize_rut

# Markers that indicate a page is genuinely a PJUD "Mis Causas" civil
# listing — used to reject garbage/unrelated HTML with a clear 4xx instead
# of silently reporting zero cases found.
_MIS_CAUSAS_MARKERS = ("detalleMisCausaCivil", "Total de registros")


class IngestParseError(ValueError):
    """Raised when the supplied HTML does not look like a PJUD Mis Causas listing."""


def _looks_like_mis_causas_html(html: str) -> bool:
    return any(marker in html for marker in _MIS_CAUSAS_MARKERS)


def _parse_filed_at(value: Optional[str]) -> Optional[datetime]:
    """Parse a PJUD ``fecha_ingreso`` (``DD/MM/YYYY``) into a ``datetime``.

    Returns ``None`` when the value is missing or unparseable — the ROL-year
    ordering in ``get_pending_detail`` covers rows with a NULL ``filed_at``,
    so a parse miss degrades gracefully rather than blocking ingest.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y")
    except (ValueError, AttributeError):
        return None


def _rol_year(rol: str) -> Optional[int]:
    """Extract the 4-digit year from a ``C-<n>-<year>`` ROL, or ``None``."""
    match = re.search(r"-(\d{4})$", rol)
    return int(match.group(1)) if match else None


class IngestService:
    """Parses raw PJUD HTML relayed by the extension and bulk-persists cases."""

    def __init__(self, db: Session):
        self.db = db

    def ingest_cases(self, *, lawyer_rut: str, competencia: str, pages: List[str]) -> dict:
        """Parse ``pages`` (raw Mis Causas HTML) and bulk-insert new cases.

        Returns ``{"new": int, "existing": int, "errors": list[str]}``.
        Raises ``IngestParseError`` when none of the supplied pages look like
        a genuine Mis Causas listing — nothing is persisted in that case.
        """
        if competencia != "civil":
            raise IngestParseError(f"Unsupported competencia for ingest: {competencia!r}")

        if not pages or not any(_looks_like_mis_causas_html(p) for p in pages):
            raise IngestParseError(
                "HTML payload does not match the expected Mis Causas listing structure"
            )

        scraper = CivilScraper(headless=True)
        parsed = []
        for html in pages:
            if not _looks_like_mis_causas_html(html):
                continue
            parsed.extend(scraper._parse_cases_html(html))

        # Dedup within the payload itself (same case can repeat across pages).
        seen = set()
        unique = []
        for c in parsed:
            rol = c.rol.strip().upper()
            if rol not in seen:
                seen.add(rol)
                unique.append(c)

        lawyer = _get_or_create_lawyer(self.db, rut=lawyer_rut)
        lawyer_id = int(lawyer.id)

        existing_rols = {
            r for (r,) in self.db.query(Case.rol).filter(Case.lawyer_id == lawyer_id)
        }
        new_cases = [c for c in unique if c.rol.strip().upper() not in existing_rols]
        result = {"new": 0, "existing": len(unique) - len(new_cases), "errors": []}

        if not new_cases:
            return result

        sync = SyncService(self.db)
        courts: dict = {}
        now = datetime.utcnow()
        mappings = []
        for c in new_cases:
            name = (c.tribunal or "Desconocido").strip() or "Desconocido"
            if name not in courts:
                court = sync._get_or_create_court(name, competencia)
                courts[name] = court.id
            plaintiff, defendant = sync._parse_caratulado(c.caratulado)
            mappings.append(
                {
                    "lawyer_id": lawyer_id,
                    "court_id": courts[name],
                    "rol": c.rol.strip().upper(),
                    "competencia": competencia,
                    "plaintiff": plaintiff,
                    "defendant": defendant,
                    "procedure": (c.cuaderno or None),
                    "status": "active",
                    "filed_at": _parse_filed_at(c.fecha_ingreso),
                    "created_at": now,
                    "updated_at": now,
                }
            )

        try:
            self.db.bulk_insert_mappings(Case, mappings)
            self.db.commit()
            result["new"] = len(mappings)
        except IntegrityError:
            # Race-safe upsert: another writer inserted one of these rols
            # concurrently. Roll back, re-check what's actually in the DB
            # now, and retry with only the rols that are still missing.
            self.db.rollback()
            existing_rols_retry = {
                r for (r,) in self.db.query(Case.rol).filter(Case.lawyer_id == lawyer_id)
            }
            retry_mappings = [m for m in mappings if m["rol"] not in existing_rols_retry]
            skipped = len(mappings) - len(retry_mappings)
            if retry_mappings:
                self.db.bulk_insert_mappings(Case, retry_mappings)
            self.db.commit()
            result["new"] = len(retry_mappings)
            result["existing"] += skipped

        return result

    def get_pending_detail(
        self, *, lawyer_rut: str, competencia: str, limit: int
    ) -> List[dict]:
        """Return the stalest cases for *lawyer_rut* needing a movements refresh.

        Ordered ``last_detail_checked_at ASC NULLS FIRST`` (never-checked first),
        then by the YEAR embedded in the ROL descending, then
        ``filed_at DESC NULLS LAST``. The ROL-year tiebreak is what keeps recent
        active cases ahead of old/closed ones: ``filed_at`` is NULL on every
        imported case, so without it the batch order among the pending pool is
        effectively arbitrary and grabs 2006–2023 causes the extension cannot
        resolve in the live Mis Causas list.

        Returns ``[]`` when the lawyer is unknown (no cases ingested yet) —
        does NOT create a lawyer (this is a read path).
        """
        lawyer = (
            self.db.query(Lawyer).filter(Lawyer.rut == normalize_rut(lawyer_rut)).first()
        )
        if lawyer is None:
            return []

        rol_year = self._rol_year_expr()

        cases = (
            self.db.query(Case)
            .filter(Case.lawyer_id == lawyer.id, Case.competencia == competencia)
            .order_by(
                Case.last_detail_checked_at.asc().nullsfirst(),
                rol_year.desc(),
                Case.filed_at.desc().nullslast(),
            )
            .limit(limit)
            .all()
        )
        return [{"id": int(c.id), "rol": c.rol} for c in cases]

    def _rol_year_expr(self):
        """SQL expression yielding the 4-digit year in a ``C-<n>-<year>`` ROL.

        ROLs that do not match are treated as year 0 (lowest priority). Uses a
        Postgres POSIX-regex ``substring`` in production; falls back to the
        trailing four characters on SQLite (test suite), which has no regex.
        """
        try:
            dialect = self.db.get_bind().dialect.name
        except Exception:
            dialect = "unknown"

        if dialect == "postgresql":
            return func.coalesce(
                cast(func.substring(Case.rol, r"-(\d{4})$"), Integer), 0
            )
        # SQLite / others (tests): the year is the last four chars; a
        # non-numeric tail casts to 0, matching the "unparseable → 0" rule.
        return func.coalesce(cast(func.substr(Case.rol, -4), Integer), 0)

    def ingest_movements(
        self,
        *,
        lawyer_rut: str,
        competencia: str,
        cases: List[dict],
        failed_rols: Optional[List[str]] = None,
    ) -> dict:
        """Parse raw detail-modal HTML relayed by the extension and persist movements.

        For each ``{"rol": ..., "html": ...}`` entry: resolve the existing
        ``Case`` by ``(rol, lawyer_id)`` (no case creation here — cases come
        from the Slice 1 ``/ingest/cases`` path), parse movements via
        ``CivilScraper._parse_case_detail_html``/``_parse_movements_table``
        (no browser), persist via ``SyncService.sync_movements``, stamp
        ``last_detail_checked_at``, and recompute the deadline/semáforo via
        ``DeadlineEngine.recompute_case`` (through the same
        ``_maybe_recompute_deadlines`` safe-fail wrapper used by the live
        scraper sync path) so previously-untracked cases get classified.

        A per-case failure (unknown ROL, malformed HTML) is recorded in
        ``errors`` and the batch continues — never raises, never persists a
        partial case (skip vs. commit is per-case, not per-batch).

        ``failed_rols`` are ROLs the extension could NOT resolve in the live
        Mis Causas list (typically old/closed causes) — they carry no detail
        HTML. For each one belonging to this lawyer+competencia, stamp
        ``last_detail_checked_at`` (no movements, no deadline recompute) so it
        rotates to the back of the batch instead of clogging every future
        ``get_pending_detail`` page. ``failed_stamped`` reports how many rotated.

        Returns
        ``{"cases_processed", "movements_new", "classified", "failed_stamped", "errors"}``.
        """
        result = {
            "cases_processed": 0,
            "movements_new": 0,
            "classified": 0,
            "failed_stamped": 0,
            "errors": [],
        }

        if competencia != "civil":
            result["errors"].append(f"Unsupported competencia for ingest: {competencia!r}")
            return result

        lawyer = (
            self.db.query(Lawyer).filter(Lawyer.rut == normalize_rut(lawyer_rut)).first()
        )
        if lawyer is None:
            result["errors"].append(f"Unknown lawyer rut: {lawyer_rut}")
            return result

        scraper = CivilScraper(headless=True)
        sync = SyncService(self.db)

        for item in cases:
            rol = (item.get("rol") or "").strip().upper()
            html = item.get("html") or ""

            case = (
                self.db.query(Case)
                .filter(Case.lawyer_id == lawyer.id, Case.rol == rol)
                .first()
            )
            if case is None:
                result["errors"].append(f"Unknown case rol for lawyer: {rol!r}")
                continue

            try:
                detail = scraper._parse_case_detail_html(html, case_token="")
            except Exception as exc:
                result["errors"].append(f"Failed to parse detail for {rol}: {exc}")
                continue

            scraped_movements = convert_api_movements_to_scraped(
                [
                    {
                        "folio": m.folio,
                        "fecha": m.fecha,
                        "tipo_tramite": m.tipo_tramite,
                        "descripcion": m.descripcion,
                        "etapa": m.etapa,
                        "foja": m.foja,
                        "tiene_documento": m.tiene_documento,
                    }
                    for m in detail.movements
                ]
            )

            new_count = 0
            if scraped_movements:
                new_count, _alerts = sync.sync_movements(
                    case_id=int(case.id), scraped_movements=scraped_movements
                )

            # Persist document tokens (Slice 3). Must run AFTER sync_movements so
            # Movement rows exist for the folio-based lookup inside
            # persist_from_detail. Idempotent on pjud_token_hash — re-ingesting the
            # same detail never duplicates Document rows. Failure isolation: a
            # document-persistence error must not poison the movement commit.
            try:
                DocumentPersistenceService().persist_from_detail(
                    detail, int(case.id), self.db
                )
            except Exception as exc:  # noqa: BLE001 — document persistence is best-effort
                result["errors"].append(
                    f"Document persistence failed for {rol}: {exc}"
                )

            was_unclassified = case.semaforo is None
            case.last_detail_checked_at = datetime.utcnow()
            _maybe_recompute_deadlines(self.db, case)
            self.db.commit()

            result["cases_processed"] += 1
            result["movements_new"] += new_count
            if was_unclassified and case.semaforo is not None:
                result["classified"] += 1

        # Rotate un-fetchable ROLs to the back of the batch: stamp them so they
        # stop refilling every pending-detail page. No movements, no deadline
        # recompute — these have no detail to parse.
        #
        # Systemic-failure guard: when NOTHING in the batch succeeded, a RUT /
        # session mismatch (popup RUT ≠ the logged-in PJUD lawyer) is the likely
        # cause — the extension asked for lawyer A's ROLs but paginated lawyer
        # B's live list, so *every* ROL "fails". pending-detail is recent-first,
        # so a mismatch always surfaces recent, valuable cases; rotating them
        # would wrongly stamp a whole active caseload. In that case rotate ONLY
        # clearly-old causes (closed, low-harm) so the all-old tail still clears
        # while recent cases keep their priority. A single success proves the
        # session is valid, so then every failed ROL rotates normally.
        session_confirmed = result["cases_processed"] > 0
        current_year = datetime.utcnow().year
        stamped_any = False
        for raw_rol in failed_rols or []:
            rol = (raw_rol or "").strip().upper()
            if not rol:
                continue
            if not session_confirmed:
                year = _rol_year(rol)
                if year is None or year >= current_year - 1:
                    continue
            case = (
                self.db.query(Case)
                .filter(
                    Case.lawyer_id == lawyer.id,
                    Case.competencia == competencia,
                    Case.rol == rol,
                )
                .first()
            )
            if case is None:
                continue
            case.last_detail_checked_at = datetime.utcnow()
            result["failed_stamped"] += 1
            stamped_any = True

        if stamped_any:
            self.db.commit()

        return result

    def ingest_documents(
        self,
        *,
        lawyer_rut: str,
        competencia: str,
        documents: List[dict],
        storage_service=None,
    ) -> dict:
        """Store PDF binaries relayed by the extension against pending Documents.

        The extension downloads each PDF in-browser (riding the live PJUD
        session while the JWT is still fresh) and POSTs the base64 bytes here.
        Each item carries the STABLE identity ``token_hash`` — identical to
        ``Document.pjud_token_hash`` = ``sha256(doc_type|case_rol|scope_key)`` —
        so the row is matched without relying on the rotating JWT.

        For each item:
        - Resolve the lawyer's ``Case`` by ``rol``.
        - Match the ``Document`` by ``(case_id, pjud_token_hash == token_hash)``.
        - Decode ``base64`` → bytes and upload via StorageService (sets
          ``gcs_path`` + ``status="stored"``); set ``filename``,
          ``content_type``, ``size_bytes``, and ``document_date``.

        Failure isolation (never raise, never poison the batch): a missing
        case/document or invalid base64 is collected into ``errors`` and counted
        in ``skipped``; the loop continues.

        Returns ``{"documents_stored", "skipped", "errors"}``.
        """
        result = {"documents_stored": 0, "skipped": 0, "errors": []}

        lawyer = (
            self.db.query(Lawyer).filter(Lawyer.rut == normalize_rut(lawyer_rut)).first()
        )
        if lawyer is None:
            # Skip every item — the lawyer has ingested nothing yet.
            for _ in documents:
                result["skipped"] += 1
            result["errors"].append(f"Unknown lawyer rut: {lawyer_rut}")
            return result

        if storage_service is None:
            from app.services.storage_service import StorageService, get_storage_backend

            storage_service = StorageService(get_storage_backend())

        for item in documents:
            rol = (item.get("rol") or "").strip().upper()
            token_hash = (item.get("token_hash") or "").strip()

            case = (
                self.db.query(Case)
                .filter(Case.lawyer_id == lawyer.id, Case.rol == rol)
                .first()
            )
            if case is None:
                result["skipped"] += 1
                result["errors"].append(f"Unknown case rol for lawyer: {rol!r}")
                continue

            doc = (
                self.db.query(Document)
                .filter(
                    Document.case_id == case.id,
                    Document.pjud_token_hash == token_hash,
                )
                .first()
            )
            if doc is None:
                result["skipped"] += 1
                result["errors"].append(
                    f"No document matched token_hash {token_hash[:12]}… for {rol}"
                )
                continue

            try:
                pdf_bytes = base64.b64decode(item.get("base64") or "", validate=True)
            except (binascii.Error, ValueError) as exc:
                result["skipped"] += 1
                result["errors"].append(
                    f"Invalid base64 for {rol} (hash {token_hash[:12]}…): {exc}"
                )
                continue

            if not pdf_bytes:
                result["skipped"] += 1
                result["errors"].append(
                    f"Empty document payload for {rol} (hash {token_hash[:12]}…)"
                )
                continue

            content_type = item.get("content_type") or "application/pdf"
            try:
                doc.filename = item.get("filename")
                doc.content_type = content_type
                doc.size_bytes = len(pdf_bytes)
                parsed_date = _parse_filed_at(item.get("document_date"))
                if parsed_date is not None:
                    doc.document_date = parsed_date
                # StorageService.upload sets doc.gcs_path + doc.status="stored"
                # (idempotent: skips re-writing an existing key).
                storage_service.upload(doc, pdf_bytes, content_type)
                self.db.commit()
            except Exception as exc:  # noqa: BLE001 — isolate per-document failures
                self.db.rollback()
                result["skipped"] += 1
                result["errors"].append(
                    f"Failed to store document for {rol} "
                    f"(hash {token_hash[:12]}…): {exc}"
                )
                continue

            result["documents_stored"] += 1

        return result
