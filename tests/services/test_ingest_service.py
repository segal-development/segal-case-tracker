"""Tests for IngestService.ingest_cases — the bulk PJUD ingest path.

Mirrors the fast bulk-insert approach from scripts/import_cases_html.py
(preload existing rols + SyncService._get_or_create_court +
bulk_insert_mappings) instead of the slow per-case SyncService.sync_cases.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
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

    def test_populates_filed_at_from_fecha_ingreso(self, db):
        service = IngestService(db)
        service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        # _case_row seeds fecha "01/01/2026" (DD/MM/YYYY).
        assert case.filed_at is not None
        assert case.filed_at.year == 2026
        assert case.filed_at.month == 1
        assert case.filed_at.day == 1

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


# ---------------------------------------------------------------------------
# get_pending_detail — Slice 2
# ---------------------------------------------------------------------------


def _detail_html(rol: str, folio: str, descripcion: str, fecha: str) -> str:
    return f"""
    <html><body>
      <table>
        <tr><td><strong>ROL:</strong> {rol}</td></tr>
        <tr><td><strong>Tribunal:</strong> 1 Juzgado Civil de Santiago</td></tr>
      </table>
      <div id="historiaCiv">
        <div class="panel panel-default">
          <div class="table-responsive">
            <table class="table table-bordered table-striped table-hover">
              <thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Etapa</th>
              <th>Tramite</th><th>Desc.</th><th>Fecha</th><th>Foja</th><th>Geo</th></tr></thead>
              <tbody>
                <tr>
                  <td>{folio}</td>
                  <td align="left"></td>
                  <td align="center"></td>
                  <td>En tramitacion</td>
                  <td>Resolucion</td>
                  <td></td>
                  <td>{descripcion}</td>
                  <td>{fecha}</td>
                  <td>1</td>
                  <td></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </body></html>
    """


MOVEMENTS_MALFORMED_HTML = "<html><body>Not a PJUD detail page.</body></html>"


@pytest.fixture
def seeded_lawyer_and_case(db):
    lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
    db.add(lawyer)
    db.flush()

    court = Court(code="T1-ING-MOV", name="Juzgado Ingest Movements Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1234-2026",
        competencia="civil",
        status="active",
    )
    db.add(case)
    db.commit()
    return {"lawyer": lawyer, "case": case}


class TestGetPendingDetail:
    def test_returns_stalest_first_nulls_first(self, db):
        lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="T1-PD", name="Juzgado Pending Detail Test", region="RM", type="civil")
        db.add(court)
        db.flush()

        now = datetime.utcnow()
        never_checked = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-1111-2026",
            competencia="civil", status="active", last_detail_checked_at=None,
        )
        older_checked = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-2222-2026",
            competencia="civil", status="active",
            last_detail_checked_at=now - timedelta(days=5),
        )
        recently_checked = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-3333-2026",
            competencia="civil", status="active",
            last_detail_checked_at=now - timedelta(hours=1),
        )
        db.add_all([never_checked, older_checked, recently_checked])
        db.commit()

        service = IngestService(db)
        result = service.get_pending_detail(lawyer_rut="11111111-1", competencia="civil", limit=30)

        assert [c["rol"] for c in result] == ["C-1111-2026", "C-2222-2026", "C-3333-2026"]
        assert all("id" in c for c in result)

    def test_respects_limit(self, db):
        lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="T1-PD2", name="Juzgado Pending Detail Test 2", region="RM", type="civil")
        db.add(court)
        db.flush()
        for i in range(3):
            db.add(Case(
                lawyer_id=lawyer.id, court_id=court.id, rol=f"C-{i}-2026",
                competencia="civil", status="active",
            ))
        db.commit()

        service = IngestService(db)
        result = service.get_pending_detail(lawyer_rut="11111111-1", competencia="civil", limit=1)
        assert len(result) == 1

    def test_unknown_lawyer_returns_empty_list(self, db):
        service = IngestService(db)
        result = service.get_pending_detail(lawyer_rut="99999999-9", competencia="civil", limit=30)
        assert result == []

    def test_recent_year_before_old_year_when_filed_at_null(self, db):
        """With last_detail_checked_at and filed_at both NULL, ordering must
        fall back to the ROL year (descending) so recent active cases come
        before old/closed ones instead of being effectively random."""
        lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="T1-YR", name="Juzgado Year Order Test", region="RM", type="civil")
        db.add(court)
        db.flush()

        old = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-100-2006",
            competencia="civil", status="active",
            last_detail_checked_at=None, filed_at=None,
        )
        mid = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-500-2018",
            competencia="civil", status="active",
            last_detail_checked_at=None, filed_at=None,
        )
        recent = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-6086-2026",
            competencia="civil", status="active",
            last_detail_checked_at=None, filed_at=None,
        )
        # Insert in non-sorted order so the result reflects ORDER BY, not insert order.
        db.add_all([mid, old, recent])
        db.commit()

        service = IngestService(db)
        result = service.get_pending_detail(lawyer_rut="11111111-1", competencia="civil", limit=30)

        assert [c["rol"] for c in result] == ["C-6086-2026", "C-500-2018", "C-100-2006"]


# ---------------------------------------------------------------------------
# ingest_movements — Slice 2
# ---------------------------------------------------------------------------


class TestIngestMovements:
    def test_valid_detail_html_persists_movements_stamps_and_classifies(
        self, db, seeded_lawyer_and_case
    ):
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                }
            ],
        )

        assert result["cases_processed"] == 1
        assert result["movements_new"] == 1
        assert result["classified"] == 1
        assert result["errors"] == []

        movements = db.query(Movement).all()
        assert len(movements) == 1
        assert movements[0].description == "Se dicta resolucion"

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        assert case.last_detail_checked_at is not None
        assert case.semaforo is not None

    def test_no_duplicate_movements_on_repeat_ingest(self, db, seeded_lawyer_and_case):
        service = IngestService(db)
        html = _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026")
        service.ingest_movements(
            lawyer_rut="11111111-1", competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )
        result = service.ingest_movements(
            lawyer_rut="11111111-1", competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )

        assert result["movements_new"] == 0
        assert db.query(Movement).count() == 1

    def test_unknown_rol_is_skipped_with_error(self, db, seeded_lawyer_and_case):
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[
                {
                    "rol": "C-9999-2026",
                    "html": _detail_html("C-9999-2026", "1", "Se dicta resolucion", "15/01/2026"),
                }
            ],
        )
        assert result["cases_processed"] == 0
        assert len(result["errors"]) == 1

    def test_malformed_html_is_graceful_no_raise(self, db, seeded_lawyer_and_case):
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": MOVEMENTS_MALFORMED_HTML}],
        )
        assert result["cases_processed"] == 0
        assert len(result["errors"]) == 1

    def test_unknown_lawyer_returns_error_no_crash(self, db):
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="99999999-9",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                }
            ],
        )
        assert result["cases_processed"] == 0
        assert len(result["errors"]) == 1


class TestIngestMovementsFailedRols:
    def test_failed_rols_are_stamped_without_movements(self, db, seeded_lawyer_and_case):
        """Un-fetchable ROLs the extension could not resolve are stamped so they
        rotate to the back of the batch — no movements, no classification."""
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[],
            failed_rols=["C-1234-2026"],
        )

        assert result["failed_stamped"] == 1
        assert result["cases_processed"] == 0
        assert result["movements_new"] == 0
        assert db.query(Movement).count() == 0

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        assert case.last_detail_checked_at is not None
        assert case.semaforo is None  # never classified — no detail was parsed

    def test_failed_rols_unknown_to_lawyer_are_ignored(self, db, seeded_lawyer_and_case):
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[],
            failed_rols=["C-0000-2099"],
        )
        assert result["failed_stamped"] == 0

    def test_failed_rols_defaults_to_empty(self, db, seeded_lawyer_and_case):
        """Backward compatibility: omitting failed_rols behaves as before."""
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                }
            ],
        )
        assert result["failed_stamped"] == 0
        assert result["cases_processed"] == 1
