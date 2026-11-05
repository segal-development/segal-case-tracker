"""T3c: ``sync_cases`` deduplicates by identity ``(rol, court_id, procedure)``
across owners and creates new cases under the firm lawyer (Approach C).

Before: the lookup was ``{rol: Case}`` scoped to the syncing lawyer, so lawyer B
syncing a causa lawyer A already created made a second row, and the same ROL in
two courts / two cuadernos overwrote one row.
"""
import re
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sync_history import SyncHistory
from app.services.sync_service import ScrapedCase, SyncService

FIRM_RUT = "16021492-9"
COURT_1 = "1º Juzgado Civil de Santiago"
COURT_2 = "2º Juzgado Civil de Santiago"


@pytest.fixture
def world(db):
    firm = Lawyer(rut=FIRM_RUT, name="Firm", is_active=True)
    a = Lawyer(rut="11111111-1", name="Lawyer A", is_active=True)
    b = Lawyer(rut="22222222-2", name="Lawyer B", is_active=True)
    db.add_all([firm, a, b])
    db.commit()
    return firm, a, b


def _scraped(rol="C-100-2025", tribunal=COURT_1, cuaderno="Principal", **kw):
    defaults = dict(
        rol=rol,
        tribunal=tribunal,
        caratulado="BANCO ITAU/FERNANDEZ",
        fecha_ingreso="01/01/2025",
        estado_cuaderno="Tramitación",
        cuaderno=cuaderno,
    )
    defaults.update(kw)
    return ScrapedCase(**defaults)


def _sync(db, lawyer, scraped, competencia="civil"):
    return SyncService(db).sync_cases(lawyer.id, scraped, competencia=competencia)


def _rows(db):
    return db.query(Case).order_by(Case.id).all()


@contextmanager
def count_case_selects(db):
    engine = db.get_bind()
    pattern = re.compile(r"\bFROM\s+cases\b", re.IGNORECASE)
    counter = {"count": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        s = statement.strip()
        if s[:6].upper() == "SELECT" and pattern.search(s):
            counter["count"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


class TestCrossLawyerDedup:
    def test_second_lawyer_does_not_create_a_second_row(self, db, world):
        firm, a, b = world
        _sync(db, a, [_scraped()])
        result = _sync(db, b, [_scraped()])

        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0].lawyer_id == firm.id
        assert (result.cases_new, result.cases_updated) == (0, 1)
        sightings = {
            s.lawyer_id for s in db.query(CaseLawyerSource).filter_by(case_id=rows[0].id)
        }
        assert sightings == {a.id, b.id}

    def test_new_cases_are_owned_by_the_firm_not_the_syncing_lawyer(self, db, world):
        firm, a, _ = world
        result = _sync(db, a, [_scraped()])
        assert result.cases_new == 1
        assert _rows(db)[0].lawyer_id == firm.id


class TestIdentityKey:
    def test_same_rol_in_two_courts_makes_two_rows(self, db, world):
        _, a, _ = world
        result = _sync(db, a, [_scraped(tribunal=COURT_1), _scraped(tribunal=COURT_2)])
        rows = _rows(db)
        assert len(rows) == 2
        assert len({r.court_id for r in rows}) == 2
        assert result.cases_new == 2

    def test_principal_and_medida_prejudicial_do_not_overwrite_each_other(self, db, world):
        _, a, _ = world
        result = _sync(
            db,
            a,
            [
                _scraped(cuaderno="Principal"),
                _scraped(cuaderno="Medida prejudicial", caratulado="OTRO/DEUDOR"),
            ],
        )
        rows = _rows(db)
        assert sorted(r.procedure for r in rows) == ["Medida prejudicial", "Principal"]
        by_proc = {r.procedure: r for r in rows}
        assert by_proc["Principal"].plaintiff == "BANCO ITAU"
        assert by_proc["Medida prejudicial"].plaintiff == "OTRO"
        assert result.cases_new == 2

    def test_none_and_empty_procedure_are_the_same_identity(self, db, world):
        firm, a, _ = world
        court = Court(code="X", name=COURT_1, region="RM", type="civil")
        db.add(court)
        db.flush()
        db.add(
            Case(
                lawyer_id=firm.id, court_id=court.id, rol="C-100-2025",
                competencia="civil", plaintiff="P", defendant="D",
                status="active", procedure=None,
            )
        )
        db.commit()
        result = _sync(db, a, [_scraped(cuaderno="")])
        assert len(_rows(db)) == 1
        assert (result.cases_new, result.cases_updated) == (0, 1)

    def test_same_batch_duplicate_resolves_to_the_created_row(self, db, world):
        _, a, _ = world
        result = _sync(db, a, [_scraped(), _scraped()])
        assert len(_rows(db)) == 1
        assert (result.cases_new, result.cases_updated) == (1, 1)

    def test_match_ignores_competencia(self, db, world):
        _, a, _ = world
        _sync(db, a, [_scraped()], competencia="civil")
        result = _sync(db, a, [_scraped()], competencia="laboral")
        # Same court name resolves to the same court, so same identity.
        assert len(_rows(db)) == 1
        assert result.cases_updated == 1


class TestIdempotence:
    def test_resync_creates_nothing_and_counts_updated(self, db, world):
        _, a, _ = world
        batch = [_scraped(rol=f"C-{i}-2025") for i in range(1, 4)]
        first = _sync(db, a, batch)
        second = _sync(db, a, batch)
        assert (first.cases_new, first.cases_updated) == (3, 0)
        assert (second.cases_new, second.cases_updated) == (0, 3)
        assert len(_rows(db)) == 3


class TestLegacyRows:
    def _legacy(self, db, owner, court_name=COURT_1, procedure="Principal", rol="C-100-2025"):
        court = db.query(Court).filter_by(name=court_name).first()
        if court is None:
            court = Court(code=court_name[:4], name=court_name, region="RM", type="civil")
            db.add(court)
            db.flush()
        case = Case(
            lawyer_id=owner.id, court_id=court.id, rol=rol, competencia="civil",
            plaintiff="OLD", defendant="OLD", status="active", procedure=procedure,
        )
        db.add(case)
        db.commit()
        return case

    def test_individually_owned_legacy_row_is_updated_and_keeps_owner(self, db, world):
        _, a, b = world
        legacy = self._legacy(db, a)
        _sync(db, b, [_scraped()])
        db.refresh(legacy)
        assert len(_rows(db)) == 1
        assert legacy.lawyer_id == a.id
        assert legacy.plaintiff == "BANCO ITAU"
        assert db.query(CaseLawyerSource).filter_by(
            case_id=legacy.id, lawyer_id=b.id
        ).count() == 1

    def test_lowest_id_wins_among_legacy_duplicates(self, db, world):
        _, a, b = world
        first = self._legacy(db, a)
        second = self._legacy(db, b)
        assert first.id < second.id
        result = _sync(db, a, [_scraped()])
        db.refresh(first)
        db.refresh(second)
        assert first.plaintiff == "BANCO ITAU"
        assert second.plaintiff == "OLD"
        assert (result.cases_new, result.cases_updated) == (0, 1)
        assert not result.errors


class TestYearFloor:
    def test_floor_skips_only_new_pre_floor_identities(self, db, world):
        _, a, b = world
        old_court = Court(code="OLD", name=COURT_1, region="RM", type="civil")
        db.add(old_court)
        db.flush()
        existing = Case(
            lawyer_id=a.id, court_id=old_court.id, rol="C-5-2015", competencia="civil",
            plaintiff="OLD", defendant="OLD", status="active", procedure="Principal",
        )
        db.add(existing)
        db.commit()

        result = _sync(
            db,
            b,
            [
                _scraped(rol="C-5-2015"),                      # known identity: updated
                _scraped(rol="C-5-2015", tribunal=COURT_2),    # new identity, old year: skipped
                _scraped(rol="C-7-2015"),                      # new, old year: skipped
                _scraped(rol="C-8-2025"),                      # new, ok: created
            ],
        )
        assert result.cases_year_skipped == 2
        assert (result.cases_new, result.cases_updated) == (1, 1)
        assert sorted(r.rol for r in _rows(db)) == ["C-5-2015", "C-8-2025"]


class TestNoFirmLawyer:
    def test_raises_and_persists_nothing(self, db):
        a = Lawyer(rut="11111111-1", name="Lawyer A", is_active=True)
        db.add(a)
        db.commit()
        with pytest.raises(RuntimeError):
            _sync(db, a, [_scraped()])
        db.rollback()
        assert db.query(Case).count() == 0
        assert db.query(Court).count() == 0
        assert db.query(SyncHistory).count() == 0


class TestQueryCountDoesNotScale:
    def _case_selects(self, db, lawyer, n):
        batch = [_scraped(rol=f"C-{i}-2025") for i in range(1, n + 1)]
        with count_case_selects(db) as counter:
            _sync(db, lawyer, batch)
        return counter["count"]

    def test_selects_on_cases_are_constant_in_n(self, db, world):
        _, a, b = world
        small = self._case_selects(db, a, 10)
        # Fresh ROLs for the big run so both start from "all new".
        big_batch = [_scraped(rol=f"C-{i}-2024") for i in range(1, 51)]
        with count_case_selects(db) as counter:
            _sync(db, b, big_batch)
        assert counter["count"] <= small + 1, (small, counter["count"])


class TestDetectCourtMatch:
    @pytest.mark.asyncio
    async def test_movements_go_to_the_row_whose_court_matches(self, db, world):
        from app.services.sync_service import detect_and_sync_movements

        firm, a, _ = world
        c1 = Court(code="C1", name=COURT_1, region="RM", type="civil")
        c2 = Court(code="C2", name=COURT_2, region="RM", type="civil")
        db.add_all([c1, c2])
        db.flush()
        rows = []
        for court in (c1, c2):
            case = Case(
                lawyer_id=firm.id, court_id=court.id, rol="C-100-2025",
                competencia="civil", plaintiff="P", defendant="D",
                status="active", procedure="Principal",
            )
            db.add(case)
            db.flush()
            db.add(CaseLawyerSource(case_id=case.id, lawyer_id=a.id))
            rows.append(case)
        db.commit()
        first, second = rows
        assert first.id < second.id

        api = MagicMock()
        api.rol = "C-100-2025"
        api.case_token = "tok"
        api.tribunal = "2 JUZGADO CIVIL DE SANTIAGO"
        api.cuaderno = "Principal"

        mv = MagicMock()
        mv.folio, mv.fecha, mv.tipo_tramite = "001", "01/06/2024", "Resolución"
        mv.descripcion, mv.etapa, mv.foja = "Auto", "Cuaderno Principal", "1"
        mv.tiene_documento, mv.documento_token, mv.documentos = False, None, []
        detail = MagicMock()
        detail.case_documents = []
        detail.movements = [mv]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=detail)

        new, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(),
            lawyer_id=a.id, api_cases=[api],
        )
        assert new == 1 and not errors
        assert db.query(Movement).filter(Movement.case_id == second.id).count() == 1
        assert db.query(Movement).filter(Movement.case_id == first.id).count() == 0


class TestConcurrentCreate:
    def test_identity_created_concurrently_is_updated_and_chunk_survives(self, db, world):
        firm, a, _ = world
        court = Court(code="X1", name=COURT_1, region="RM", type="civil")
        db.add(court)
        db.flush()
        db.add(
            Case(
                lawyer_id=firm.id, court_id=court.id, rol="C-5-2025",
                competencia="civil", plaintiff="OLD", defendant="OLD",
                status="active", procedure="Principal",
            )
        )
        db.commit()

        svc = SyncService(db)
        svc._load_cases_by_identity = lambda rols: {}  # stale preload: C-5 looks new
        result = svc.sync_cases(
            a.id, [_scraped(rol="C-6-2025"), _scraped(rol="C-5-2025"), _scraped(rol="C-7-2025")]
        )

        assert not result.errors
        assert (result.cases_new, result.cases_updated) == (2, 1)
        assert sorted(r.rol for r in _rows(db)) == ["C-5-2025", "C-6-2025", "C-7-2025"]
        assert db.query(Case).filter_by(rol="C-5-2025").one().plaintiff == "BANCO ITAU"
        assert db.query(SyncHistory).one().status == "completed"
