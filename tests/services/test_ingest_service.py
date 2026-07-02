"""Tests for IngestService.ingest_cases — the bulk PJUD ingest path.

Mirrors the fast bulk-insert approach from scripts/import_cases_html.py
(preload existing rols + SyncService._get_or_create_court +
bulk_insert_mappings) instead of the slow per-case SyncService.sync_cases.
"""

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.case import Case
from app.models.lawyer import Lawyer
from app.services.ingest_service import IngestParseError, IngestService


def _case_row(token: str, rol: str, tribunal: str, caratulado: str) -> str:
    return f"""
    <tr>
      <td><a onclick="detalleMisCausaCivil('{token}')">Ver</a></td>
      <td>{rol}</td>
      <td>{tribunal}</td>
      <td>{caratulado}</td>
      <td>01/01/2026</td>
      <td>En tramitacion</td>
      <td>Principal</td>
      <td>-</td>
    </tr>
    """


def _mis_causas_page(rows: list[str]) -> str:
    return f"""
    <html><body>
      Total de registros: {len(rows)}
      <table>{''.join(rows)}</table>
    </body></html>
    """


VALID_PAGE = _mis_causas_page(
    [
        _case_row("TOKEN1", "C-1234-2026", "1 Juzgado Civil de Santiago", "BANCO DE CHILE / PEREZ"),
        _case_row("TOKEN2", "C-5678-2026", "2 Juzgado Civil de Santiago", "BCI / GONZALEZ"),
    ]
)

MALFORMED_PAGE = "<html><body>This is not a PJUD Mis Causas page.</body></html>"


class TestIngestCasesValidPayload:
    def test_persists_new_cases_and_returns_counts(self, db):
        service = IngestService(db)
        result = service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )

        assert result["new"] == 2
        assert result["existing"] == 0
        assert result["errors"] == []

        cases = db.query(Case).all()
        assert {c.rol for c in cases} == {"C-1234-2026", "C-5678-2026"}

    def test_unknown_lawyer_rut_is_get_or_create(self, db):
        assert db.query(Lawyer).filter(Lawyer.rut == "11111111-1").first() is None

        service = IngestService(db)
        service.ingest_cases(lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE])

        lawyer = db.query(Lawyer).filter(Lawyer.rut == "11111111-1").first()
        assert lawyer is not None

    def test_repeat_ingest_produces_no_duplicates(self, db):
        service = IngestService(db)
        service.ingest_cases(lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE])

        result = service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )

        assert result["new"] == 0
        assert result["existing"] == 2

        cases = db.query(Case).filter(Case.rol == "C-1234-2026").all()
        assert len(cases) == 1

    def test_malformed_html_raises_parse_error_and_persists_nothing(self, db):
        service = IngestService(db)
        with pytest.raises(IngestParseError):
            service.ingest_cases(
                lawyer_rut="11111111-1", competencia="civil", pages=[MALFORMED_PAGE]
            )

        assert db.query(Case).count() == 0
        assert db.query(Lawyer).count() == 0

    def test_concurrent_insert_race_is_handled_via_integrity_error(self, db, monkeypatch):
        """Simulate a race: another writer inserts the same rol mid-flight."""
        service = IngestService(db)

        lawyer_rut = "11111111-1"
        # Pre-create the lawyer + a pre-existing case with a DIFFERENT rol so
        # the preload snapshot is stale by the time bulk_insert_mappings runs.
        from app.utils.rut import normalize_rut
        from app.models.lawyer import Lawyer as LawyerModel

        lawyer = LawyerModel(rut=normalize_rut(lawyer_rut), name="Test", is_active=True)
        db.add(lawyer)
        db.commit()

        original_bulk_insert = db.bulk_insert_mappings
        call_count = {"n": 0}

        def _flaky_bulk_insert(model, mappings):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First attempt: simulate a concurrent writer winning the
                # unique-constraint race on the underlying insert.
                raise IntegrityError("insert", {}, Exception("uq_cases_lawyer_rol"))
            return original_bulk_insert(model, mappings)

        monkeypatch.setattr(db, "bulk_insert_mappings", _flaky_bulk_insert)

        # This should not raise — the service must catch IntegrityError and
        # retry safely (re-query + filter).
        result = service.ingest_cases(
            lawyer_rut=lawyer_rut, competencia="civil", pages=[VALID_PAGE]
        )
        assert "errors" in result
