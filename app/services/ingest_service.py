"""IngestService — bulk-persists PJUD cases relayed by the browser extension.

Adapted from ``scripts/import_cases_html.py``'s fast bulk-insert path
(preload existing rols + ``SyncService._get_or_create_court`` +
``bulk_insert_mappings``) rather than the per-case ``SyncService.sync_cases``,
which was too slow through the operator's proxy (see design ADR in
sdd/conectar-pjud-extension/design).
"""

import base64
import binascii
import logging
import re
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Integer, cast, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import firm_lawyer_id
from app.api.v1.auth import _get_or_create_lawyer
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.scrapper.pjud.civil import CivilScraper
from app.services.document_persistence import (
    DocumentPersistenceService,
    document_identity_hash,
)
from app.services.sync_service import (
    SyncService,
    _maybe_recompute_deadlines,
    convert_api_movements_to_scraped,
    upsert_escritos,
    upsert_exhortos,
    upsert_litigantes,
    upsert_notificaciones,
)
from app.utils.rut import normalize_rut

logger = logging.getLogger(__name__)

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

        Approach C (unificar-modelo-causas, ADR-001): every Case is upserted
        under the FIRM's canonical ``lawyer_id`` (``firm_lawyer_id``), never
        the syncing lawyer's own id — dedup-by-ROL is scoped to the firm id
        so the same ROL synced by different lawyers never creates a second
        Case row. The syncing lawyer's own ``Lawyer`` row is still
        get-or-created (needed for the ``case_lawyer_source`` sighting
        below, which feeds ``get_pending_detail``'s per-syncing-lawyer
        rotation). Raises ``RuntimeError`` (via ``firm_lawyer_id``) BEFORE
        any lawyer/case row is touched when the firm Lawyer is misconfigured.
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

        # Resolve the firm's canonical owner BEFORE touching any row — a
        # misconfigured FIRM_LAWYER_RUT must leave nothing persisted.
        owner_id = firm_lawyer_id(self.db)

        lawyer = _get_or_create_lawyer(self.db, rut=lawyer_rut)
        syncing_lawyer_id = int(lawyer.id)

        existing_rols = {
            r for (r,) in self.db.query(Case.rol).filter(Case.lawyer_id == owner_id)
        }
        new_cases = [c for c in unique if c.rol.strip().upper() not in existing_rols]
        result = {"new": 0, "existing": len(unique) - len(new_cases), "errors": []}

        if new_cases:
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
                        "lawyer_id": owner_id,
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
                    r for (r,) in self.db.query(Case.rol).filter(Case.lawyer_id == owner_id)
                }
                retry_mappings = [m for m in mappings if m["rol"] not in existing_rols_retry]
                skipped = len(mappings) - len(retry_mappings)
                if retry_mappings:
                    self.db.bulk_insert_mappings(Case, retry_mappings)
                self.db.commit()
                result["new"] = len(retry_mappings)
                result["existing"] += skipped

        # Record the syncing lawyer's sighting of EVERY ROL in this payload
        # (new or already-existing under the firm id) via case_lawyer_source
        # — this is what lets get_pending_detail rotate per syncing lawyer
        # even though every Case is firm-owned (ADR-002).
        all_rols = {c.rol.strip().upper() for c in unique}
        if all_rols:
            case_ids_by_rol = {
                rol: cid
                for (cid, rol) in self.db.query(Case.id, Case.rol).filter(
                    Case.lawyer_id == owner_id, Case.rol.in_(all_rols)
                )
            }
            sighted_at = datetime.utcnow()
            for case_id in case_ids_by_rol.values():
                self._upsert_case_lawyer_source(case_id, syncing_lawyer_id, sighted_at)
            self.db.commit()

        return result

    def _upsert_case_lawyer_source(
        self, case_id: int, lawyer_id: int, seen_at: datetime
    ) -> None:
        """Upsert a ``case_lawyer_source(case_id, lawyer_id)`` sighting.

        Creates the row (stamping both ``first_seen_at``/``last_seen_at``)
        the first time this lawyer sees this case, otherwise refreshes only
        ``last_seen_at``. Flush-only — caller commits.
        """
        row = (
            self.db.query(CaseLawyerSource)
            .filter(
                CaseLawyerSource.case_id == case_id,
                CaseLawyerSource.lawyer_id == lawyer_id,
            )
            .first()
        )
        if row is None:
            self.db.add(
                CaseLawyerSource(
                    case_id=case_id,
                    lawyer_id=lawyer_id,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                )
            )
        else:
            row.last_seen_at = seen_at

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
        ``{"cases_processed", "movements_new", "litigantes_new", "classified",
        "failed_stamped", "errors"}``.
        """
        result = {
            "cases_processed": 0,
            "movements_new": 0,
            "litigantes_new": 0,
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

            # Persist case entities (litigantes, notificaciones, escritos,
            # exhortos) already parsed into `detail`. Without this the case has
            # ZERO case_litigantes rows, so case_ids_for_abogado (the per-abogado
            # frontend view) cannot attribute it to any lawyer and the case is
            # invisible in that view — the extension sync's core bug.
            #
            # ADR-004: litigantes are SILENT — the upsert_* helpers deliberately
            # create no alert/notification (storage-only), so nothing is
            # dispatched here. Idempotent on (case_id, natural_key), so
            # re-ingesting the same detail never duplicates rows. Flush-only
            # (no internal commit) — covered by the single commit below.
            # Failure isolation: an entity-persistence error lands in `errors`
            # and must NOT poison the movement commit.
            try:
                lit_new = sum(
                    1 for _, is_new in upsert_litigantes(
                        self.db, int(case.id), detail.litigantes
                    ) if is_new
                )
                upsert_notificaciones(self.db, int(case.id), detail.notificaciones)
                upsert_escritos(self.db, int(case.id), detail.escritos)
                upsert_exhortos(self.db, int(case.id), detail.exhortos)
                result["litigantes_new"] += lit_new
            except Exception as exc:  # noqa: BLE001 — entity persistence is best-effort
                result["errors"].append(
                    f"Entity persistence failed for {rol}: {exc}"
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
        """Store PDF binaries relayed by the extension, upserting Document rows.

        The extension downloads each PDF in-browser (riding the live PJUD
        session while the JWT is still fresh) and POSTs the base64 bytes here.
        The extension is the AUTHORITATIVE source of a document's identity: it
        already parsed ``doc_type``, ``scope_key`` (movement folio),
        ``pjud_endpoint`` and the live ``pjud_token`` at download time, and
        sends them here. Each item carries the STABLE identity ``token_hash`` —
        identical to ``Document.pjud_token_hash`` =
        ``sha256(doc_type|case_rol|scope_key)`` — so the row is matched (or
        created) without relying on the rotating JWT.

        For each item:
        - Resolve the lawyer's ``Case`` by ``rol`` (unknown ROL → skip).
        - Decode ``base64`` → bytes (invalid/empty → skip; never a partial row).
        - Match the ``Document`` by ``(case_id, pjud_token_hash == token_hash)``.
          When NOT found, CREATE it from the extension-supplied metadata:
          ``doc_type``, ``pjud_endpoint``, ``pjud_token``,
          ``pjud_token_hash=token_hash``, ``status="pending"`` and
          ``movement_id`` resolved by matching a Movement on
          ``(case_id, folio == scope_key)`` when ``scope_key`` is non-empty.
          The supplied ``token_hash`` is verified against
          ``document_identity_hash(doc_type, case.rol, scope_key)`` — a mismatch
          is logged as a warning but still stored (defensive; the extension
          computes the same formula).
        - Upload via StorageService (sets ``gcs_path`` + ``status="stored"``);
          set ``filename``, ``content_type``, ``size_bytes`` and ``document_date``.

        Idempotency: re-POSTing the same ``token_hash`` matches the existing row
        (no duplicate) and ``StorageService.upload`` skips re-writing the key.

        Failure isolation (never raise, never poison the batch): a missing
        case, invalid base64, or storage error is collected into ``errors`` and
        counted in ``skipped``; the loop continues.

        Returns ``{"documents_stored", "documents_created", "skipped", "errors"}``.
        """
        result = {
            "documents_stored": 0,
            "documents_created": 0,
            "skipped": 0,
            "errors": [],
        }

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
            doc_type = (item.get("doc_type") or "").strip()
            scope_key = (item.get("scope_key") or "").strip()

            case = (
                self.db.query(Case)
                .filter(Case.lawyer_id == lawyer.id, Case.rol == rol)
                .first()
            )
            if case is None:
                result["skipped"] += 1
                result["errors"].append(f"Unknown case rol for lawyer: {rol!r}")
                continue

            # Decode BEFORE any row creation so an invalid payload never leaves
            # an orphan pending Document behind.
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

            doc = (
                self.db.query(Document)
                .filter(
                    Document.case_id == case.id,
                    Document.pjud_token_hash == token_hash,
                )
                .first()
            )
            created = False
            if doc is None:
                # The extension is authoritative — create the row from the
                # metadata it already parsed at download time.
                expected_hash = document_identity_hash(doc_type, case.rol, scope_key)
                if token_hash != expected_hash:
                    logger.warning(
                        "token_hash mismatch for %s (doc_type=%s, scope_key=%r): "
                        "got %s…, expected %s… — storing anyway (defensive).",
                        rol,
                        doc_type,
                        scope_key,
                        token_hash[:12],
                        expected_hash[:12],
                    )

                movement_id = None
                if scope_key:
                    movement = (
                        self.db.query(Movement)
                        .filter(
                            Movement.case_id == case.id,
                            Movement.folio == scope_key,
                        )
                        .first()
                    )
                    movement_id = movement.id if movement is not None else None

                doc = Document(
                    case_id=case.id,
                    movement_id=movement_id,
                    doc_type=doc_type or None,
                    pjud_endpoint=item.get("pjud_endpoint"),
                    pjud_token=item.get("pjud_token"),
                    pjud_token_hash=token_hash,
                    status="pending",
                )
                self.db.add(doc)
                created = True

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
            if created:
                result["documents_created"] += 1

        return result
