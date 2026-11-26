"""Causas prioritarias (S1, S2): personal per-lawyer marks with a cap of 20."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_priority import CasePriority
from app.models.court import Court
from app.models.lawyer import Lawyer

CAP_DETAIL = "Máximo 20 causas prioritarias por abogado."


@pytest.fixture
def court(db):
    obj = Court(code="T1-PC", name="Juzgado PC", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _lawyer(db, rut, name, role="lawyer"):
    obj = Lawyer(rut=rut, name=name, role=role)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def ana(db):
    return _lawyer(db, "11111111-1", "Ana")


@pytest.fixture
def beto(db):
    return _lawyer(db, "22222222-2", "Beto")


def _case(db, lawyer, court, rol, **kw):
    obj = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active",
        competencia="civil", plaintiff="Banco", defendant="Juan",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **kw,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _as(lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}
    app.dependency_overrides[get_current_lawyer] = _mock


@pytest.fixture
def as_ana(client, ana):
    _as(ana)
    return client


def _marked(client):
    r = client.get("/api/v1/priority-cases")
    assert r.status_code == 200
    return [i["case_id"] for i in r.json()["items"]]


def _mark_many(db, lawyer, cases):
    base = datetime(2026, 1, 1)
    for n, c in enumerate(cases):
        db.add(CasePriority(case_id=c.id, lawyer_id=lawyer.id, created_at=base + timedelta(minutes=n)))
    db.commit()


def _own_cases(db, lawyer, court, n, start=0):
    return [_case(db, lawyer, court, f"C-{start + i}-2025") for i in range(n)]


class TestMarkOne:
    def test_put_marks_and_lists(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        r = as_ana.put(f"/api/v1/cases/{c.id}/priority")
        assert r.status_code == 204
        assert r.content == b""
        assert _marked(as_ana) == [c.id]

    def test_put_is_idempotent(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        assert as_ana.put(f"/api/v1/cases/{c.id}/priority").status_code == 204
        assert as_ana.put(f"/api/v1/cases/{c.id}/priority").status_code == 204
        data = as_ana.get("/api/v1/priority-cases").json()
        assert data["total"] == 1
        assert db.query(CasePriority).count() == 1

    def test_put_idempotent_at_cap_does_not_409(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 20)
        _mark_many(db, ana, cases)
        r = as_ana.put(f"/api/v1/cases/{cases[0].id}/priority")
        assert r.status_code == 204
        assert as_ana.get("/api/v1/priority-cases").json()["total"] == 20

    def test_put_out_of_scope_404_nothing_saved(self, as_ana, db, beto, court):
        other = _case(db, beto, court, "C-9-2025")
        r = as_ana.put(f"/api/v1/cases/{other.id}/priority")
        assert r.status_code == 404
        assert _marked(as_ana) == []
        assert db.query(CasePriority).count() == 0

    def test_put_unknown_case_404(self, as_ana):
        assert as_ana.put("/api/v1/cases/999999/priority").status_code == 404

    def test_delete_unmarks(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        as_ana.put(f"/api/v1/cases/{c.id}/priority")
        r = as_ana.delete(f"/api/v1/cases/{c.id}/priority")
        assert r.status_code == 204
        assert _marked(as_ana) == []

    def test_delete_unmarked_is_204(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        assert as_ana.delete(f"/api/v1/cases/{c.id}/priority").status_code == 204
        assert _marked(as_ana) == []


class TestCap:
    def test_21st_mark_is_409_with_exact_text_and_nothing_saved(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 21)
        for c in cases[:20]:
            assert as_ana.put(f"/api/v1/cases/{c.id}/priority").status_code == 204
        before = _marked(as_ana)
        r = as_ana.put(f"/api/v1/cases/{cases[20].id}/priority")
        assert r.status_code == 409
        assert r.json()["detail"] == CAP_DETAIL
        assert _marked(as_ana) == before
        assert cases[20].id not in _marked(as_ana)

    def test_unmark_frees_a_slot(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 21)
        for c in cases[:20]:
            as_ana.put(f"/api/v1/cases/{c.id}/priority")
        as_ana.delete(f"/api/v1/cases/{cases[0].id}/priority")
        assert as_ana.put(f"/api/v1/cases/{cases[20].id}/priority").status_code == 204
        assert as_ana.get("/api/v1/priority-cases").json()["total"] == 20


class TestBulk:
    def test_bulk_on_marks_all(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 3)
        r = as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": [c.id for c in cases], "on": True})
        assert r.status_code == 200
        assert r.json() == {"total": 3}
        assert sorted(_marked(as_ana)) == sorted(c.id for c in cases)

    def test_bulk_on_with_already_marked_is_idempotent(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 3)
        as_ana.put(f"/api/v1/cases/{cases[0].id}/priority")
        ids = [c.id for c in cases] + [cases[0].id]
        r = as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": ids, "on": True})
        assert r.status_code == 200
        assert r.json() == {"total": 3}

    def test_bulk_off_unmarks_and_ignores_unmarked(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 3)
        as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": [cases[0].id, cases[1].id], "on": True})
        r = as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": [cases[1].id, cases[2].id], "on": False})
        assert r.status_code == 200
        assert r.json() == {"total": 1}
        assert _marked(as_ana) == [cases[0].id]

    def test_bulk_over_cap_is_409_and_nothing_saved(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 25)
        _mark_many(db, ana, cases[:18])
        before = _marked(as_ana)
        r = as_ana.post(
            "/api/v1/priority-cases/bulk",
            json={"case_ids": [c.id for c in cases[15:21]], "on": True},  # 15-17 marked, 18-20 new => 21
        )
        assert r.status_code == 409
        assert r.json()["detail"] == CAP_DETAIL
        assert _marked(as_ana) == before

    def test_bulk_exactly_at_cap_is_ok(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 20)
        r = as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": [c.id for c in cases], "on": True})
        assert r.status_code == 200
        assert r.json() == {"total": 20}

    def test_bulk_any_out_of_scope_is_404_and_nothing_saved(self, as_ana, db, ana, beto, court):
        mine = _case(db, ana, court, "C-1-2025")
        theirs = _case(db, beto, court, "C-2-2025")
        r = as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": [mine.id, theirs.id], "on": True})
        assert r.status_code == 404
        assert _marked(as_ana) == []

    def test_bulk_off_out_of_scope_is_404_and_nothing_removed(self, as_ana, db, ana, beto, court):
        mine = _case(db, ana, court, "C-1-2025")
        theirs = _case(db, beto, court, "C-2-2025")
        as_ana.put(f"/api/v1/cases/{mine.id}/priority")
        r = as_ana.post("/api/v1/priority-cases/bulk", json={"case_ids": [mine.id, theirs.id], "on": False})
        assert r.status_code == 404
        assert _marked(as_ana) == [mine.id]


class TestPersonal:
    def test_each_lawyer_sees_only_own_marks(self, client, db, ana, beto, court):
        shared = _case(db, ana, court, "C-1-2025")
        from app.models.case_litigante import CaseLitigante
        db.add(CaseLitigante(case_id=shared.id, participante="AB.DDO", rut=beto.rut,
                             persona_type="NATURAL", nombre="Beto", natural_key="k-beto"))
        db.add(CaseLitigante(case_id=shared.id, participante="AB.DDO", rut=ana.rut,
                             persona_type="NATURAL", nombre="Ana", natural_key="k-ana"))
        db.commit()
        _as(ana)
        assert client.put(f"/api/v1/cases/{shared.id}/priority").status_code == 204
        _as(beto)
        assert _marked(client) == []
        assert client.get(f"/api/v1/cases/{shared.id}").json()["case"]["is_priority"] is False
        # Beto's delete must not remove Ana's mark
        assert client.delete(f"/api/v1/cases/{shared.id}/priority").status_code == 204
        _as(ana)
        assert _marked(client) == [shared.id]

    def test_cap_is_per_lawyer(self, client, db, ana, beto, court):
        _mark_many(db, ana, _own_cases(db, ana, court, 20))
        b = _case(db, beto, court, "C-B-2025")
        _as(beto)
        assert client.put(f"/api/v1/cases/{b.id}/priority").status_code == 204

    def test_auditor_has_own_marks_and_full_scope(self, client, db, ana, court):
        aud = _lawyer(db, "33333333-3", "Aud", role="auditor")
        c = _case(db, ana, court, "C-1-2025")
        _as(aud)
        assert client.put(f"/api/v1/cases/{c.id}/priority").status_code == 204
        assert _marked(client) == [c.id]
        _as(ana)
        assert _marked(client) == []


class TestListShape:
    def test_item_fields_order_and_max(self, as_ana, db, ana, court):
        c1 = _case(db, ana, court, "C-1-2025", semaforo="rojo",
                   last_detail_checked_at=datetime(2026, 5, 1, 10, 0))
        c2 = _case(db, ana, court, "C-2-2025")
        _mark_many(db, ana, [c1, c2])  # c2 marked later, but c1 is rojo (S6 order)
        data = as_ana.get("/api/v1/priority-cases").json()
        assert data["max"] == 20
        assert data["total"] == 2
        assert [i["case_id"] for i in data["items"]] == [c1.id, c2.id]
        item = data["items"][0]
        assert set(item) == {
            "case_id", "rol", "caratula", "tribunal", "semaforo",
            "last_detail_checked_at", "marcada_at", "ultimo_movimiento", "movimiento_nuevo",
        }
        assert item["rol"] == "C-1-2025"
        assert item["caratula"] == "Banco/Juan"
        assert item["tribunal"] == "Juzgado PC"
        assert item["semaforo"] == "rojo"
        assert item["last_detail_checked_at"].startswith("2026-05-01T10:00")
        assert item["marcada_at"].startswith("2026-01-01T00:00")

    def test_empty_list(self, as_ana):
        assert as_ana.get("/api/v1/priority-cases").json() == {
            "items": [], "total": 0, "max": 20, "con_movimiento_nuevo": 0, "ultima_revision": None,
        }

    def test_mark_leaving_scope_is_not_listed(self, as_ana, db, ana, beto, court):
        c = _case(db, ana, court, "C-1-2025")
        as_ana.put(f"/api/v1/cases/{c.id}/priority")
        c.lawyer_id = beto.id
        db.commit()
        assert _marked(as_ana) == []


class TestIsPriority:
    def test_list_and_detail_flag(self, as_ana, db, ana, court):
        a = _case(db, ana, court, "C-1-2025")
        b = _case(db, ana, court, "C-2-2025")
        as_ana.put(f"/api/v1/cases/{a.id}/priority")
        items = {i["id"]: i for i in as_ana.get("/api/v1/cases").json()["items"]}
        assert items[a.id]["is_priority"] is True
        assert items[b.id]["is_priority"] is False
        assert as_ana.get(f"/api/v1/cases/{a.id}").json()["case"]["is_priority"] is True
        assert as_ana.get(f"/api/v1/cases/{b.id}").json()["case"]["is_priority"] is False

    def test_list_uses_one_priority_query_regardless_of_size(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 6)
        _mark_many(db, ana, cases[:3])
        seen = []

        def spy(conn, cursor, statement, *a):
            if "case_priority" in statement:
                seen.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", spy)
        try:
            r = as_ana.get("/api/v1/cases")
        finally:
            event.remove(engine, "before_cursor_execute", spy)
        assert r.status_code == 200
        assert len(r.json()["items"]) == 6
        assert len(seen) == 1

    def test_existing_list_response_unchanged_otherwise(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025", semaforo="verde")
        data = as_ana.get("/api/v1/cases").json()
        assert data["total"] == 1 and data["page"] == 1
        item = data["items"][0]
        assert item["id"] == c.id
        assert item["rol"] == "C-1-2025"
        assert item["semaforo"] == "verde"
        assert item["court"]["name"] == "Juzgado PC"
        assert item["is_priority"] is False
        assert "ruts" in item and "abogado_nombre" in item


# --- S6: ultimo_movimiento / movimiento_nuevo / counters / ordering -----------------

from app.models.alert import Alert  # noqa: E402
from app.models.movement import Movement  # noqa: E402


def _mov(db, case, when, desc="Mov"):
    m = Movement(case_id=case.id, description=desc, movement_date=when, folio="1")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _alert(db, lawyer, case, movement=None, read=False, type_="new_movement"):
    a = Alert(
        lawyer_id=lawyer.id, case_id=case.id, movement_id=movement.id if movement else None,
        type=type_, title="t", read=read,
    )
    db.add(a)
    db.commit()
    return a


def _items(client):
    return {i["case_id"]: i for i in client.get("/api/v1/priority-cases").json()["items"]}


class TestUltimoMovimiento:
    def test_picks_latest_movement_and_date_only(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        _mov(db, c, datetime(2026, 3, 1, 9, 0), "Antiguo")
        _mov(db, c, datetime(2026, 4, 5, 15, 30), "Reciente")
        _mov(db, c, datetime(2026, 2, 1, 9, 0), "Mas antiguo")
        _mark_many(db, ana, [c])
        assert _items(as_ana)[c.id]["ultimo_movimiento"] == {
            "descripcion": "Reciente", "fecha": "2026-04-05",
        }

    def test_null_without_movements(self, as_ana, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        _mark_many(db, ana, [c])
        assert _items(as_ana)[c.id]["ultimo_movimiento"] is None


class TestMovimientoNuevo:
    def _setup(self, db, ana, court):
        c = _case(db, ana, court, "C-1-2025")
        m = _mov(db, c, datetime(2026, 4, 1))
        _mark_many(db, ana, [c])
        return c, m

    def test_unread_movement_alert_is_true(self, as_ana, db, ana, court):
        c, m = self._setup(db, ana, court)
        _alert(db, ana, c, m)
        assert _items(as_ana)[c.id]["movimiento_nuevo"] is True

    def test_no_alert_is_false(self, as_ana, db, ana, court):
        c, _ = self._setup(db, ana, court)
        assert _items(as_ana)[c.id]["movimiento_nuevo"] is False

    def test_read_alert_is_false(self, as_ana, db, ana, court):
        c, m = self._setup(db, ana, court)
        _alert(db, ana, c, m, read=True)
        assert _items(as_ana)[c.id]["movimiento_nuevo"] is False

    def test_other_lawyers_alert_is_false(self, as_ana, db, ana, beto, court):
        c, m = self._setup(db, ana, court)
        _alert(db, beto, c, m)
        assert _items(as_ana)[c.id]["movimiento_nuevo"] is False

    def test_non_movement_alert_is_false(self, as_ana, db, ana, court):
        c, _ = self._setup(db, ana, court)
        _alert(db, ana, c, None, type_="plazo_fatal")
        assert _items(as_ana)[c.id]["movimiento_nuevo"] is False


class TestTopLevelAndOrdering:
    def test_counters(self, as_ana, db, ana, court):
        a = _case(db, ana, court, "C-1-2025", last_detail_checked_at=datetime(2026, 5, 1, 10, 0))
        b = _case(db, ana, court, "C-2-2025", last_detail_checked_at=datetime(2026, 6, 1, 12, 0))
        c = _case(db, ana, court, "C-3-2025")
        _mark_many(db, ana, [a, b, c])
        _alert(db, ana, a, _mov(db, a, datetime(2026, 4, 1)))
        _alert(db, ana, c, _mov(db, c, datetime(2026, 4, 1)))
        data = as_ana.get("/api/v1/priority-cases").json()
        assert data["con_movimiento_nuevo"] == 2
        assert data["ultima_revision"].startswith("2026-06-01T12:00")

    def test_empty_counters(self, as_ana):
        data = as_ana.get("/api/v1/priority-cases").json()
        assert data["con_movimiento_nuevo"] == 0
        assert data["ultima_revision"] is None
        assert data["items"] == []

    def test_order_semaforo_then_movement_desc_nulls_last_then_marcada(self, as_ana, db, ana, court):
        v = _case(db, ana, court, "C-V-2025", semaforo="verde")
        n = _case(db, ana, court, "C-N-2025")
        a_old = _case(db, ana, court, "C-AO-2025", semaforo="amarillo")
        a_new = _case(db, ana, court, "C-AN-2025", semaforo="amarillo")
        a_none1 = _case(db, ana, court, "C-A1-2025", semaforo="amarillo")
        a_none2 = _case(db, ana, court, "C-A2-2025", semaforo="amarillo")
        r = _case(db, ana, court, "C-R-2025", semaforo="rojo")
        g = _case(db, ana, court, "C-G-2025", semaforo="gris")
        _mov(db, a_old, datetime(2026, 1, 1))
        _mov(db, a_new, datetime(2026, 3, 1))
        _mov(db, v, datetime(2026, 9, 1))
        # marked order (later = marcada newer): a_none1 before a_none2
        _mark_many(db, ana, [v, n, a_old, a_new, a_none1, a_none2, r, g])
        ids = [i["case_id"] for i in as_ana.get("/api/v1/priority-cases").json()["items"]]
        # rest rank (gris / null) ties, newest mark first: g was marked after n
        assert ids == [r.id, a_new.id, a_old.id, a_none2.id, a_none1.id, v.id, g.id, n.id]

    def test_bounded_queries(self, as_ana, db, ana, court):
        cases = _own_cases(db, ana, court, 6)
        for c in cases:
            _alert(db, ana, c, _mov(db, c, datetime(2026, 4, 1)))
        _mark_many(db, ana, cases)
        seen = {"alerts": 0, "movements": 0}

        def spy(conn, cursor, statement, *a):
            s = statement.lower()
            if "from alerts" in s:
                seen["alerts"] += 1
            if "from movements" in s:
                seen["movements"] += 1

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", spy)
        try:
            r = as_ana.get("/api/v1/priority-cases")
        finally:
            event.remove(engine, "before_cursor_execute", spy)
        assert r.status_code == 200 and len(r.json()["items"]) == 6
        assert seen["alerts"] <= 1
        assert seen["movements"] <= 1
