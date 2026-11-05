"""T3c: ``IngestService.ingest_cases`` detects existing cases by identity
``(rol, court_id, procedure)``, not by ROL alone."""
import pytest

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.lawyer import Lawyer
from app.services.ingest_service import IngestService

FIRM_RUT = "16021492-9"
LAWYER_RUT = "11111111-1"


@pytest.fixture(autouse=True)
def _firm(db):
    db.add(Lawyer(rut=FIRM_RUT, name="Firm", is_active=True))
    db.commit()


def _row(token, rol, tribunal, cuaderno="Principal"):
    return f"""
    <tr>
      <td><a onclick="detalleMisCausaCivil('{token}')">Ver</a></td>
      <td>{rol}</td>
      <td>{tribunal}</td>
      <td>BANCO / PEREZ</td>
      <td>01/01/2026</td>
      <td>En tramitacion</td>
      <td>{cuaderno}</td>
      <td>-</td>
    </tr>
    """


def _page(rows):
    return f"""<html><body>Total de registros: {len(rows)}
    <table>{''.join(rows)}</table></body></html>"""


def _ingest(db, rows):
    return IngestService(db).ingest_cases(
        lawyer_rut=LAWYER_RUT, competencia="civil", pages=[_page(rows)]
    )


TWO_COURTS = [
    _row("T1", "C-100-2026", "1 Juzgado Civil de Santiago"),
    _row("T2", "C-100-2026", "2 Juzgado Civil de Santiago"),
]
TWO_CUADERNOS = [
    _row("T1", "C-200-2026", "1 Juzgado Civil de Santiago", "Principal"),
    _row("T2", "C-200-2026", "1 Juzgado Civil de Santiago", "Medida prejudicial"),
]


class TestIngestIdentity:
    @pytest.mark.parametrize("rows", [TWO_COURTS, TWO_CUADERNOS], ids=["courts", "cuadernos"])
    def test_same_rol_makes_two_rows(self, db, rows):
        result = _ingest(db, rows)
        assert result["new"] == 2
        assert db.query(Case).count() == 2

    @pytest.mark.parametrize("rows", [TWO_COURTS, TWO_CUADERNOS], ids=["courts", "cuadernos"])
    def test_reingest_inserts_nothing(self, db, rows):
        _ingest(db, rows)
        result = _ingest(db, rows)
        assert result["new"] == 0
        assert result["existing"] == 2
        assert db.query(Case).count() == 2

    @pytest.mark.parametrize("rows", [TWO_COURTS, TWO_CUADERNOS], ids=["courts", "cuadernos"])
    def test_sighting_rows_cover_every_identity(self, db, rows):
        _ingest(db, rows)
        lawyer = db.query(Lawyer).filter_by(rut=LAWYER_RUT).one()
        case_ids = {c.id for c in db.query(Case)}
        sighted = {
            s.case_id for s in db.query(CaseLawyerSource).filter_by(lawyer_id=lawyer.id)
        }
        assert len(case_ids) == 2
        assert sighted == case_ids

    def test_new_identity_of_known_rol_is_inserted(self, db):
        _ingest(db, [TWO_COURTS[0]])
        result = _ingest(db, TWO_COURTS)
        assert result["new"] == 1
        assert result["existing"] == 1
        assert db.query(Case).count() == 2

    def test_exact_duplicate_row_in_payload_is_inserted_once(self, db):
        result = _ingest(db, [TWO_COURTS[0], TWO_COURTS[0]])
        assert result["new"] == 1
        assert db.query(Case).count() == 1
