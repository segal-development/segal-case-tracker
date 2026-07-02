"""IngestService — bulk-persists PJUD cases relayed by the browser extension.

Adapted from ``scripts/import_cases_html.py``'s fast bulk-insert path
(preload existing rols + ``SyncService._get_or_create_court`` +
``bulk_insert_mappings``) rather than the per-case ``SyncService.sync_cases``,
which was too slow through the operator's proxy (see design ADR in
sdd/conectar-pjud-extension/design).
"""

from datetime import datetime
from typing import List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.auth import _get_or_create_lawyer
from app.models.case import Case
from app.scrapper.pjud.civil import CivilScraper
from app.services.sync_service import SyncService

# Markers that indicate a page is genuinely a PJUD "Mis Causas" civil
# listing — used to reject garbage/unrelated HTML with a clear 4xx instead
# of silently reporting zero cases found.
_MIS_CAUSAS_MARKERS = ("detalleMisCausaCivil", "Total de registros")


class IngestParseError(ValueError):
    """Raised when the supplied HTML does not look like a PJUD Mis Causas listing."""


def _looks_like_mis_causas_html(html: str) -> bool:
    return any(marker in html for marker in _MIS_CAUSAS_MARKERS)


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
