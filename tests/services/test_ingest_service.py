"""Tests for IngestService.ingest_cases — the bulk PJUD ingest path.

Mirrors the fast bulk-insert approach from scripts/import_cases_html.py
(preload existing rols + SyncService._get_or_create_court +
bulk_insert_mappings) instead of the slow per-case SyncService.sync_cases.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.ingest_service import IngestParseError, IngestService

FIRM_RUT = "16021492-9"


@pytest.fixture(autouse=True)
def _seed_firm_lawyer(db):
    """Seed the firm's canonical Lawyer row (FIRM_LAWYER_RUT default).

    Approach C (unificar-modelo-causas, PR1b): ingest_cases/ingest_movements
    upsert Case rows under the firm lawyer_id, not the syncing lawyer's own
    id, so firm_lawyer_id(db) must resolve for every ingest test in this
    module. Tests that need to exercise the "missing firm lawyer" error
    monkeypatch FIRM_LAWYER_RUT to a RUT with no matching row instead.
    """
    existing = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()
    if existing is None:
        db.add(Lawyer(rut=FIRM_RUT, name="Firm Lawyer", is_active=True))
        db.commit()


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
        # Only the pre-seeded firm lawyer exists — the parse error is raised
        # before the syncing lawyer's own row would be get-or-created.
        assert db.query(Lawyer).count() == 1
        assert db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).count() == 1

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


class TestIngestCasesFirmOwnership:
    """Task 1b-2/1b-3: ingest_cases upserts one Case per ROL under the firm
    lawyer_id, never the syncing lawyer's own id, and records the syncing
    lawyer's sighting via case_lawyer_source."""

    def test_ingest_new_rol_creates_case_under_firm_lawyer_id(self, db):
        service = IngestService(db)
        service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )

        firm = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()
        cases = db.query(Case).all()
        assert len(cases) == 2
        assert all(c.lawyer_id == firm.id for c in cases)

    def test_ingest_existing_rol_under_firm_upserts_no_duplicate(self, db):
        service = IngestService(db)
        service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )
        # A DIFFERENT syncing lawyer ingests the same ROLs — dedup is scoped
        # by the firm id, not by which lawyer is syncing.
        result = service.ingest_cases(
            lawyer_rut="22222222-2", competencia="civil", pages=[VALID_PAGE]
        )

        assert result["new"] == 0
        assert result["existing"] == 2
        assert db.query(Case).filter(Case.rol == "C-1234-2026").count() == 1

    def test_ingest_missing_firm_lawyer_raises_clear_error(self, db, monkeypatch):
        monkeypatch.setenv("FIRM_LAWYER_RUT", "99999999-0")
        service = IngestService(db)

        with pytest.raises(RuntimeError, match="FIRM_LAWYER_RUT"):
            service.ingest_cases(
                lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
            )

        assert db.query(Case).count() == 0

    def test_ingest_writes_case_lawyer_source_for_syncing_lawyer(self, db):
        service = IngestService(db)
        service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )

        syncing = db.query(Lawyer).filter(Lawyer.rut == "11111111-1").first()
        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        source = (
            db.query(CaseLawyerSource)
            .filter(
                CaseLawyerSource.case_id == case.id,
                CaseLawyerSource.lawyer_id == syncing.id,
            )
            .first()
        )
        assert source is not None
        assert source.last_seen_at is not None

    def test_ingest_existing_rol_updates_last_seen_at_on_source_row(self, db):
        service = IngestService(db)
        service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )
        syncing = db.query(Lawyer).filter(Lawyer.rut == "11111111-1").first()
        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        source = (
            db.query(CaseLawyerSource)
            .filter(
                CaseLawyerSource.case_id == case.id,
                CaseLawyerSource.lawyer_id == syncing.id,
            )
            .first()
        )
        first_seen = source.first_seen_at
        original_last_seen = source.last_seen_at

        service.ingest_cases(
            lawyer_rut="11111111-1", competencia="civil", pages=[VALID_PAGE]
        )

        db.refresh(source)
        assert source.first_seen_at == first_seen
        assert source.last_seen_at >= original_last_seen
        # Still exactly one source row for this (case, lawyer) pair.
        assert (
            db.query(CaseLawyerSource)
            .filter(
                CaseLawyerSource.case_id == case.id,
                CaseLawyerSource.lawyer_id == syncing.id,
            )
            .count()
            == 1
        )


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
    """Case is FIRM-owned (Approach C) — ingest_movements resolves cases by
    the firm lawyer_id, never the syncing lawyer's own id. ``lawyer`` here is
    the SYNCING lawyer passed as ``lawyer_rut`` to ingest_movements calls."""
    lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
    db.add(lawyer)
    db.flush()

    firm = db.query(Lawyer).filter(Lawyer.rut == FIRM_RUT).first()

    court = Court(code="T1-ING-MOV", name="Juzgado Ingest Movements Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=firm.id,
        court_id=court.id,
        rol="C-1234-2026",
        competencia="civil",
        status="active",
    )
    db.add(case)
    db.commit()
    return {"lawyer": lawyer, "case": case, "firm": firm}


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


class TestIngestMovementsFirmOwnership:
    """Task 1b-4: ingest_movements resolves the FIRM-owned Case (never the
    syncing lawyer's own id) and records the syncing lawyer's sighting via
    case_lawyer_source."""

    def test_ingest_movements_resolves_firm_owned_case(self, db, seeded_lawyer_and_case):
        """A DIFFERENT lawyer than the one who originally synced the case can
        still resolve+process it via ingest_movements, because the Case is
        firm-owned, not owned by whichever lawyer first synced it."""
        other_syncer = Lawyer(rut="33333333-3", name="Other Syncer", is_active=True)
        db.add(other_syncer)
        db.commit()

        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="33333333-3",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                }
            ],
        )

        assert result["cases_processed"] == 1
        assert result["errors"] == []

    def test_ingest_movements_writes_case_lawyer_source(self, db, seeded_lawyer_and_case):
        lawyer = seeded_lawyer_and_case["lawyer"]
        case = seeded_lawyer_and_case["case"]

        service = IngestService(db)
        service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html("C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"),
                }
            ],
        )

        source = (
            db.query(CaseLawyerSource)
            .filter(
                CaseLawyerSource.case_id == case.id,
                CaseLawyerSource.lawyer_id == lawyer.id,
            )
            .first()
        )
        assert source is not None
        assert source.last_seen_at is not None


class TestIngestMovementsFailedRols:
    def test_failed_rols_stamped_when_session_confirmed(self, db, seeded_lawyer_and_case):
        """With the session confirmed (>=1 case processed) even a RECENT
        un-fetchable ROL is stamped so it rotates to the back — no movements,
        no classification. Age only matters on a systemic (zero-success) batch."""
        firm = seeded_lawyer_and_case["firm"]
        court = db.query(Court).first()
        recent_failed = f"C-5555-{datetime.utcnow().year}"
        db.add(Case(lawyer_id=firm.id, court_id=court.id, rol=recent_failed,
                    competencia="civil", status="active"))
        db.commit()

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
            failed_rols=[recent_failed],
        )

        assert result["cases_processed"] == 1
        assert result["failed_stamped"] == 1
        assert db.query(Movement).count() == 1  # only the processed case

        failed = db.query(Case).filter(Case.rol == recent_failed).first()
        assert failed.last_detail_checked_at is not None
        assert failed.semaforo is None  # never classified — no detail was parsed

    def test_failed_rols_rotation_resolves_firm_case_not_syncing_lawyer(
        self, db, seeded_lawyer_and_case
    ):
        """Task 1b-4: failed_rols rotation resolves the case by the FIRM
        lawyer_id, not the syncing lawyer's own id — a case a DIFFERENT
        lawyer originally synced (still firm-owned) still rotates correctly
        when reported as failed by this syncing lawyer."""
        firm = seeded_lawyer_and_case["firm"]
        court = db.query(Court).first()
        db.add(Case(lawyer_id=firm.id, court_id=court.id, rol="C-100-2006",
                    competencia="civil", status="active"))
        db.commit()

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
            failed_rols=["C-100-2006"],
        )

        assert result["failed_stamped"] == 1
        case = db.query(Case).filter(Case.rol == "C-100-2006").first()
        assert case.last_detail_checked_at is not None
        assert case.lawyer_id == firm.id  # never re-pointed to the syncing lawyer

    def test_recent_failed_rols_not_stamped_on_systemic_failure(self, db, seeded_lawyer_and_case):
        """RUT/session-mismatch guard: when NOTHING in the batch succeeds, recent
        ROLs keep their priority. A mismatch (wrong RUT for the session) surfaces
        recent, valuable ROLs — stamping them would rotate a whole active caseload."""
        firm = seeded_lawyer_and_case["firm"]
        court = db.query(Court).first()
        recent_rol = f"C-7777-{datetime.utcnow().year}"
        db.add(Case(lawyer_id=firm.id, court_id=court.id, rol=recent_rol,
                    competencia="civil", status="active"))
        db.commit()

        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[],
            failed_rols=[recent_rol],
        )

        assert result["cases_processed"] == 0
        assert result["failed_stamped"] == 0
        case = db.query(Case).filter(Case.rol == recent_rol).first()
        assert case.last_detail_checked_at is None  # protected

    def test_old_failed_rols_stamped_even_on_systemic_failure(self, db, seeded_lawyer_and_case):
        """The all-old tail still clears: with zero successes, clearly-old (closed)
        ROLs rotate so they stop clogging every pending-detail page."""
        firm = seeded_lawyer_and_case["firm"]
        court = db.query(Court).first()
        db.add(Case(lawyer_id=firm.id, court_id=court.id, rol="C-100-2006",
                    competencia="civil", status="active"))
        db.commit()

        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[],
            failed_rols=["C-100-2006"],
        )

        assert result["cases_processed"] == 0
        assert result["failed_stamped"] == 1
        case = db.query(Case).filter(Case.rol == "C-100-2006").first()
        assert case.last_detail_checked_at is not None

    def test_failed_rols_unknown_to_lawyer_are_ignored(self, db, seeded_lawyer_and_case):
        """An old ROL not belonging to the lawyer is a no-op: it passes the age
        guard but the lookup finds no matching case to stamp."""
        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[],
            failed_rols=["C-0000-2006"],
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


# ---------------------------------------------------------------------------
# ingest_movements — Document row creation (Slice 3)
# ---------------------------------------------------------------------------


def _detail_html_with_doc(
    rol: str,
    folio: str,
    descripcion: str,
    fecha: str,
    token: str = "eyJhbGciOiJub25lIn0.e30.DOC_TOK",
) -> str:
    """Detail HTML whose single movement carries a docuS (resolution) document.

    The Doc. column (cell 1) contains a ``dtaDoc`` hidden input plus the
    ``docuS.php`` marker, which is what ``_parse_movements_table`` keys on to
    emit a ``resolution`` PJUDDocument.
    """
    doc_cell = (
        f'<form action="documentos/docuS.php">'
        f'<input type="hidden" name="dtaDoc" value="{token}">'
        f"</form>"
    )
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
                  <td align="left">{doc_cell}</td>
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


class TestIngestMovementsDocuments:
    def test_document_bearing_html_creates_pending_document(
        self, db, seeded_lawyer_and_case
    ):
        from app.services.document_persistence import document_identity_hash

        service = IngestService(db)
        service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html_with_doc(
                        "C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"
                    ),
                }
            ],
        )

        docs = db.query(Document).all()
        assert len(docs) == 1
        doc = docs[0]

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        movement = db.query(Movement).filter(Movement.case_id == case.id).first()

        assert doc.case_id == case.id
        assert doc.movement_id == movement.id
        assert doc.doc_type == "resolution"
        assert doc.status == "pending"
        assert doc.pjud_endpoint == "documentos/docuS.php"
        # Stable identity hash = sha256(doc_type|case_rol|folio)
        assert doc.pjud_token_hash == document_identity_hash(
            "resolution", "C-1234-2026", "1"
        )
        # document_date mirrors the movement date (15/01/2026)
        assert doc.document_date is not None
        assert doc.document_date.year == 2026
        assert doc.document_date.month == 1
        assert doc.document_date.day == 15

    def test_re_ingest_does_not_duplicate_document(
        self, db, seeded_lawyer_and_case
    ):
        service = IngestService(db)
        html = _detail_html_with_doc(
            "C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"
        )
        service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )
        service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )

        assert db.query(Document).count() == 1


# ---------------------------------------------------------------------------
# ingest_movements — Case entity (litigantes) persistence
# ---------------------------------------------------------------------------


def _litigante_row(participante: str, rut: str, persona_type: str, nombre: str) -> str:
    return f"""
    <tr>
      <td>{participante}</td>
      <td>{rut}</td>
      <td>{persona_type}</td>
      <td>{nombre}</td>
    </tr>
    """


def _detail_html_with_litigantes(
    rol: str,
    folio: str,
    descripcion: str,
    fecha: str,
    litigantes: list[str],
) -> str:
    """Detail HTML carrying BOTH a movements pane (historiaCiv) and a
    Litigantes pane (litigantesCiv), mirroring the real extension payload."""
    lit_rows = "".join(litigantes)
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
      <div class="tab-pane fade" id="litigantesCiv">
        <div class="panel panel-default">
          <div class="table-responsive">
            <table class="table table-bordered table-striped table-hover">
              <thead><tr><th>Participante</th><th>Rut</th><th>Persona</th>
              <th>Nombre o Razon Social</th></tr></thead>
              <tbody>{lit_rows}</tbody>
            </table>
          </div>
        </div>
      </div>
    </body></html>
    """


class TestIngestMovementsLitigantes:
    def test_litigantes_pane_creates_case_litigante_rows(
        self, db, seeded_lawyer_and_case
    ):
        from app.models.case_litigante import CaseLitigante
        from app.services.sync_service import litigante_natural_key
        from app.scrapper.pjud.base import PJUDLitigante

        html = _detail_html_with_litigantes(
            "C-1234-2026", "1", "Se dicta resolucion", "15/01/2026",
            litigantes=[
                _litigante_row("DTE.", "76543210-K", "JURIDICA", "BANCO FICTICIO S.A."),
                _litigante_row("AB.DTE", "11111111-1", "NATURAL", "TEST LAWYER"),
                _litigante_row("DDO.", "11234567-K", "NATURAL", "ROBERTO VIDAL"),
            ],
        )

        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )

        assert result["cases_processed"] == 1
        assert result["errors"] == []

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        rows = (
            db.query(CaseLitigante)
            .filter(CaseLitigante.case_id == case.id)
            .all()
        )
        assert len(rows) == 3
        assert {r.rut for r in rows} == {"76543210-K", "11111111-1", "11234567-K"}
        assert {r.participante for r in rows} == {"DTE.", "AB.DTE", "DDO."}

        # Natural keys match the sync-layer derivation (dedup spine).
        expected_key = litigante_natural_key(
            PJUDLitigante(
                participante="AB.DTE",
                rut="11111111-1",
                persona_type="NATURAL",
                nombre="TEST LAWYER",
            )
        )
        ab_row = next(r for r in rows if r.participante == "AB.DTE")
        assert ab_row.natural_key == expected_key

    def test_re_ingest_does_not_duplicate_litigantes(
        self, db, seeded_lawyer_and_case
    ):
        from app.models.case_litigante import CaseLitigante

        html = _detail_html_with_litigantes(
            "C-1234-2026", "1", "Se dicta resolucion", "15/01/2026",
            litigantes=[
                _litigante_row("DTE.", "76543210-K", "JURIDICA", "BANCO FICTICIO S.A."),
                _litigante_row("AB.DTE", "11111111-1", "NATURAL", "TEST LAWYER"),
            ],
        )
        service = IngestService(db)
        service.ingest_movements(
            lawyer_rut="11111111-1", competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )
        service.ingest_movements(
            lawyer_rut="11111111-1", competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )

        assert db.query(CaseLitigante).count() == 2

    def test_case_becomes_visible_to_abogado_after_ingest(
        self, db, seeded_lawyer_and_case
    ):
        """The core bug: extension-synced cases must be attributable to the
        abogado via case_ids_for_abogado (the per-abogado frontend view)."""
        from app.services.lawyer_roster import case_ids_for_abogado

        html = _detail_html_with_litigantes(
            "C-1234-2026", "1", "Se dicta resolucion", "15/01/2026",
            litigantes=[
                _litigante_row("DTE.", "76543210-K", "JURIDICA", "BANCO FICTICIO S.A."),
                _litigante_row("AB.DTE", "11111111-1", "NATURAL", "TEST LAWYER"),
            ],
        )
        service = IngestService(db)
        service.ingest_movements(
            lawyer_rut="11111111-1", competencia="civil",
            cases=[{"rol": "C-1234-2026", "html": html}],
        )

        case = db.query(Case).filter(Case.rol == "C-1234-2026").first()
        ids = case_ids_for_abogado(db, "11111111-1", "11111111-1")
        assert case.id in ids

    def test_entityless_detail_still_processes_movements(
        self, db, seeded_lawyer_and_case
    ):
        """Isolation: a detail with NO litigantes pane persists movements fine
        and simply creates zero case_litigantes rows."""
        from app.models.case_litigante import CaseLitigante

        service = IngestService(db)
        result = service.ingest_movements(
            lawyer_rut="11111111-1",
            competencia="civil",
            cases=[
                {
                    "rol": "C-1234-2026",
                    "html": _detail_html(
                        "C-1234-2026", "1", "Se dicta resolucion", "15/01/2026"
                    ),
                }
            ],
        )

        assert result["cases_processed"] == 1
        assert result["movements_new"] == 1
        assert result["errors"] == []
        assert db.query(CaseLitigante).count() == 0
