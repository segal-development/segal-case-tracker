"""S6: GET /cases `movimiento_nuevo` filter and per-item flag."""

from datetime import datetime

import pytest
from sqlalchemy import event

from app.models.court import Court
from tests.api.v1 import test_priority_cases as tp


@pytest.fixture
def court(db):
    obj = Court(code="T1-MN", name="Juzgado MN", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def ana(db):
    return tp._lawyer(db, "11111111-1", "Ana")


@pytest.fixture
def beto(db):
    return tp._lawyer(db, "22222222-2", "Beto")


@pytest.fixture
def as_ana(client, ana):
    tp._as(ana)
    return client


_case, _alert, _mov, _own_cases = tp._case, tp._alert, tp._mov, tp._own_cases


def _with_alert(db, lawyer, case, **kw):
    return _alert(db, lawyer, case, _mov(db, case, datetime(2026, 4, 1)), **kw)


def _get(client, **params):
    r = client.get("/api/v1/cases", params=params)
    assert r.status_code == 200
    return r.json()


class TestFlag:
    def test_item_flag_true_only_for_unread_own_movement_alert(self, as_ana, db, ana, beto, court):
        unread = _case(db, ana, court, "C-1-2025")
        read = _case(db, ana, court, "C-2-2025")
        other = _case(db, ana, court, "C-3-2025")
        nonmov = _case(db, ana, court, "C-4-2025")
        plain = _case(db, ana, court, "C-5-2025")
        _with_alert(db, ana, unread)
        _with_alert(db, ana, read, read=True)
        _with_alert(db, beto, other)
        _alert(db, ana, nonmov, None, type_="plazo_fatal")
        items = {i["id"]: i["movimiento_nuevo"] for i in _get(as_ana)["items"]}
        assert items == {
            unread.id: True, read.id: False, other.id: False, nonmov.id: False, plain.id: False,
        }

    def test_flag_uses_one_alerts_query(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 6)
        for c in cases[:3]:
            _with_alert(db, ana, c)
        seen = []

        def spy(conn, cursor, statement, *a):
            if "from alerts" in statement.lower():
                seen.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", spy)
        try:
            data = _get(as_ana)
        finally:
            event.remove(engine, "before_cursor_execute", spy)
        assert len(data["items"]) == 6
        assert len(seen) == 1


class TestFilter:
    def test_true_returns_only_unread_causas_and_total(self, as_ana, db, ana, beto, court):
        a = _case(db, ana, court, "C-1-2025")
        b = _case(db, ana, court, "C-2-2025")
        c = _case(db, ana, court, "C-3-2025")
        _with_alert(db, ana, a)
        _with_alert(db, ana, b, read=True)
        _with_alert(db, beto, c)
        data = _get(as_ana, movimiento_nuevo="true")
        assert [i["id"] for i in data["items"]] == [a.id]
        assert data["total"] == 1
        assert data["items"][0]["movimiento_nuevo"] is True

    def test_two_alerts_same_case_no_duplicates(self, as_ana, db, ana, court):
        a = _case(db, ana, court, "C-1-2025")
        _with_alert(db, ana, a)
        _with_alert(db, ana, a)
        data = _get(as_ana, movimiento_nuevo="true")
        assert [i["id"] for i in data["items"]] == [a.id]
        assert data["total"] == 1

    def test_composes_with_q_and_sem(self, as_ana, db, ana, court):
        a = _case(db, ana, court, "C-1-2025", semaforo="rojo")
        b = _case(db, ana, court, "C-2-2025", semaforo="verde")
        c = _case(db, ana, court, "C-3-2025", semaforo="rojo")
        for x in (a, b):
            _with_alert(db, ana, x)
        assert [i["id"] for i in _get(as_ana, movimiento_nuevo="true", sem="rojo")["items"]] == [a.id]
        assert [i["id"] for i in _get(as_ana, movimiento_nuevo="true", q="C-2")["items"]] == [b.id]
        assert _get(as_ana, movimiento_nuevo="true", q="C-3")["total"] == 0
        assert c.id  # c has no alert

    def test_pagination_total(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 3)
        for c in cases:
            _with_alert(db, ana, c)
        _own_cases(db, ana, court, 2, start=10)
        data = _get(as_ana, movimiento_nuevo="true", per_page=2, page=2)
        assert data["total"] == 3 and data["pages"] == 2 and len(data["items"]) == 1

    def test_false_or_absent_does_not_filter(self, as_ana, db, ana, court):
        a = _case(db, ana, court, "C-1-2025")
        _case(db, ana, court, "C-2-2025")
        _with_alert(db, ana, a)
        assert _get(as_ana)["total"] == 2
        assert _get(as_ana, movimiento_nuevo="false")["total"] == 2
