"""T3c follow-up: rows of one causa that share a ROL (Principal + Medida
prejudicial) must not double-book or starve the detail rotation.

Rule: rows with the same (ROL, court_id) are siblings. The rotation picks one
per batch; a finished visit stamps ``last_detail_checked_at`` on all of them
with ONE UPDATE (timestamp only, no movements). The same ROL in ANOTHER court is
a different causa: not a sibling, picked on its own with the api_case whose
tribunal matches its court.
"""
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement

COURT_1 = "1º Juzgado Civil de Santiago"
COURT_2 = "2º Juzgado Civil de Santiago"
NOW = datetime(2026, 10, 14, 12, 0, 0)


@pytest.fixture
def world(db):
    firm = Lawyer(rut="16021492-9", name="Firm", is_active=True)
    a = Lawyer(rut="11111111-1", name="A", is_active=True)
    c1 = Court(code="C1", name=COURT_1, region="RM", type="civil")
    c2 = Court(code="C2", name=COURT_2, region="RM", type="civil")
    db.add_all([firm, a, c1, c2])
    db.commit()
    return firm, a, c1, c2


def _case(db, firm, a, court, rol, procedure="Principal", checked=None):
    case = Case(
        lawyer_id=firm.id, court_id=court.id, rol=rol, competencia="civil",
        plaintiff="P", defendant="D", status="active", procedure=procedure,
        last_detail_checked_at=checked,
    )
    db.add(case)
    db.flush()
    db.add(CaseLawyerSource(case_id=case.id, lawyer_id=a.id))
    db.commit()
    return case


def _api(rol, tribunal=COURT_1, cuaderno="Principal"):
    m = MagicMock()
    m.rol = rol
    m.case_token = "tok-" + rol
    m.tribunal = tribunal
    m.cuaderno = cuaderno
    return m


def _select(db, lawyer, apis, batch_size=10):
    from app.services.sync_service import _select_cases_for_detail_rotation

    return _select_cases_for_detail_rotation(
        db, lawyer.id, "civil", apis, batch_size=batch_size
    )


async def _detect(db, lawyer, apis):
    from app.services.sync_service import detect_and_sync_movements

    mv = MagicMock()
    mv.folio, mv.fecha, mv.tipo_tramite = "001", "01/06/2024", "Resolución"
    mv.descripcion, mv.etapa, mv.foja = "Auto", "Cuaderno Principal", "1"
    mv.tiene_documento, mv.documento_token, mv.documentos = False, None, []
    detail = MagicMock()
    detail.case_documents = []
    detail.movements = [mv]
    scraper = MagicMock()
    scraper.get_case_detail = AsyncMock(return_value=detail)
    return await detect_and_sync_movements(
        db=db, scraper=scraper, pjud_session=MagicMock(),
        lawyer_id=lawyer.id, api_cases=apis,
    )


class TestSelection:
    def test_two_rows_same_rol_and_court_are_picked_once(self, db, world):
        firm, a, c1, _ = world
        _case(db, firm, a, c1, "C-1-2026", "Principal")
        _case(db, firm, a, c1, "C-1-2026", "Medida prejudicial")
        apis = [_api("C-1-2026"), _api("C-1-2026", cuaderno="Medida prejudicial")]
        picked = _select(db, a, apis)
        assert [p.rol for p in picked] == ["C-1-2026"]

    def test_batch_cap_counts_distinct_causas(self, db, world):
        firm, a, c1, _ = world
        _case(db, firm, a, c1, "C-1-2026", "Principal")
        _case(db, firm, a, c1, "C-1-2026", "Medida prejudicial")
        _case(db, firm, a, c1, "C-2-2026")
        apis = [_api("C-1-2026"), _api("C-2-2026")]
        picked = _select(db, a, apis, batch_size=2)
        assert sorted(p.rol for p in picked) == ["C-1-2026", "C-2-2026"]

    def test_single_row_rol_unchanged(self, db, world):
        firm, a, c1, _ = world
        _case(db, firm, a, c1, "C-3-2026")
        assert [p.rol for p in _select(db, a, [_api("C-3-2026")])] == ["C-3-2026"]

    def test_same_rol_in_two_courts_picks_each_with_its_own_api_case(self, db, world):
        firm, a, c1, c2 = world
        _case(db, firm, a, c1, "C-4-2026")
        _case(db, firm, a, c2, "C-4-2026")
        apis = [_api("C-4-2026", COURT_1), _api("C-4-2026", COURT_2)]
        picked = _select(db, a, apis)
        assert sorted(p.tribunal for p in picked) == [COURT_1, COURT_2]


class TestSiblingStamp:
    @pytest.mark.asyncio
    async def test_both_rows_are_stamped_and_sibling_gets_no_movements(self, db, world):
        firm, a, c1, _ = world
        main = _case(db, firm, a, c1, "C-1-2026", "Principal")
        sib = _case(db, firm, a, c1, "C-1-2026", "Medida prejudicial")
        new, _, errors = await _detect(db, a, [_api("C-1-2026")])
        assert new == 1 and not errors
        db.expire_all()
        assert main.last_detail_checked_at is not None
        assert sib.last_detail_checked_at == main.last_detail_checked_at
        assert db.query(Movement).filter(Movement.case_id == sib.id).count() == 0

    @pytest.mark.asyncio
    async def test_next_rotation_does_not_put_the_rol_first_again(self, db, world):
        firm, a, c1, _ = world
        _case(db, firm, a, c1, "C-1-2026", "Principal")
        _case(db, firm, a, c1, "C-1-2026", "Medida prejudicial")
        other = _case(db, firm, a, c1, "C-9-2026", checked=NOW - timedelta(days=30))
        apis = [_api("C-1-2026"), _api("C-9-2026")]
        await _detect(db, a, [apis[0]])
        picked = _select(db, a, apis, batch_size=1)
        assert [p.rol for p in picked] == ["C-9-2026"]
        assert other.id

    @pytest.mark.asyncio
    async def test_different_court_row_is_not_stamped(self, db, world):
        firm, a, c1, c2 = world
        main = _case(db, firm, a, c1, "C-4-2026")
        other = _case(db, firm, a, c2, "C-4-2026")
        await _detect(db, a, [_api("C-4-2026", COURT_1)])
        db.expire_all()
        assert main.last_detail_checked_at is not None
        assert other.last_detail_checked_at is None

    @pytest.mark.asyncio
    async def test_single_row_still_stamped(self, db, world):
        firm, a, c1, _ = world
        only = _case(db, firm, a, c1, "C-3-2026")
        await _detect(db, a, [_api("C-3-2026")])
        db.expire_all()
        assert only.last_detail_checked_at is not None

    @pytest.mark.asyncio
    async def test_stamp_queries_do_not_scale_with_siblings(self, db, world):
        firm, a, c1, _ = world

        async def run(rol, n_siblings):
            _case(db, firm, a, c1, rol, "Principal")
            for i in range(n_siblings):
                _case(db, firm, a, c1, rol, f"Cuaderno {i}")
            stmts = []

            def _l(conn, cur, statement, *args):
                if statement.lstrip().upper().startswith("UPDATE CASES"):
                    stmts.append(statement)

            engine = db.get_bind()
            event.listen(engine, "before_cursor_execute", _l)
            try:
                await _detect(db, a, [_api(rol)])
            finally:
                event.remove(engine, "before_cursor_execute", _l)
            return len(stmts)

        few = await run("C-5-2026", 1)
        many = await run("C-6-2026", 8)
        assert many == few


class TestOtherCourtNotInListing:
    def test_row_of_unlisted_court_does_not_borrow_the_listed_api_case(self, db, world):
        firm, a, c1, c2 = world
        _case(db, firm, a, c2, "C-4-2026", checked=datetime(2026, 1, 1))
        _case(db, firm, a, c1, "C-4-2026", checked=datetime(2026, 2, 1))
        picked = _select(db, a, [_api("C-4-2026", COURT_1)])
        assert len(picked) == 1

    @pytest.mark.asyncio
    async def test_unlisted_court_row_cannot_hold_the_front_forever(self, db, world):
        firm, a, c1, c2 = world
        _case(db, firm, a, c2, "C-4-2026", checked=datetime(2026, 1, 1))
        _case(db, firm, a, c1, "C-4-2026", checked=datetime(2026, 2, 1))
        _case(db, firm, a, c1, "C-8-2026", checked=datetime(2026, 3, 1))
        apis = [_api("C-4-2026", COURT_1), _api("C-8-2026", COURT_1)]
        picked = _select(db, a, apis, batch_size=2)
        assert sorted(p.rol for p in picked) == ["C-4-2026", "C-8-2026"]

    def test_single_court_with_unmatched_tribunal_text_is_still_picked_once(self, db, world):
        firm, a, c1, _ = world
        _case(db, firm, a, c1, "C-4-2026", "Principal")
        _case(db, firm, a, c1, "C-4-2026", "Medida prejudicial")
        picked = _select(db, a, [_api("C-4-2026", "Nombre raro del tribunal")])
        assert [p.rol for p in picked] == ["C-4-2026"]

    @pytest.mark.asyncio
    async def test_listed_court_row_is_still_visited_after_the_skip(self, db, world):
        firm, a, c1, c2 = world
        _case(db, firm, a, c2, "C-4-2026", checked=datetime(2026, 1, 1))
        listed = _case(db, firm, a, c1, "C-4-2026", checked=datetime(2026, 2, 1))
        new, _, errors = await _detect(db, a, [_api("C-4-2026", COURT_1)])
        assert new == 1 and not errors
        db.expire_all()
        assert listed.last_detail_checked_at > datetime(2026, 2, 1)
        assert db.query(Movement).filter(Movement.case_id == listed.id).count() == 1
