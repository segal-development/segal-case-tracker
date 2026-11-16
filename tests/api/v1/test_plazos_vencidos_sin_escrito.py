"""GET /cases/plazos-vencidos-sin-escrito (Fase 0 S5) and the mark endpoint clearing the redness."""

from datetime import date, datetime, timedelta

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer

TODAY = date(2026, 11, 16)
URL = "/api/v1/cases/plazos-vencidos-sin-escrito"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def people(db):
    out = {
        role: Lawyer(rut=rut, name=f"{role} X", role=role, email=f"{role}@t.cl")
        for role, rut in [("admin", "11111111-1"), ("auditor", "22222222-2"),
                          ("lawyer", "33333333-3"), ("procurador", "44444444-4")]
    }
    db.add_all(out.values())
    db.commit()
    return out


@pytest.fixture
def court(db):
    obj = Court(code="T1", name="12º Juzgado Civil", region="RM", type="civil")
    db.add(obj)
    db.commit()
    return obj


def _login(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    return client


def _case_with_row(db, people, court, rol, due, *, marked=False, status="no_cumplido",
                   dtype="excepciones_8d"):
    c = Case(lawyer_id=people["lawyer"].id, court_id=court.id, rol=rol, status="active",
             competencia="civil", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(c)
    db.flush()
    row = CaseDeadline(case_id=c.id, deadline_type=dtype, due_date=due,
                       triggered_at=due - timedelta(days=15), status=status,
                       marked_at=datetime(2026, 11, 10) if marked else None)
    db.add(row)
    db.commit()
    return c, row


@pytest.mark.parametrize("role", ["admin", "auditor"])
def test_admin_and_auditor_get_the_list_newest_first(client, db, people, court, role):
    _, old = _case_with_row(db, people, court, "C-1-2026", TODAY - timedelta(days=90))
    c_new, new = _case_with_row(db, people, court, "C-2-2026", date(2026, 10, 19))
    _case_with_row(db, people, court, "C-3-2026", date(2026, 10, 1), marked=True)
    _case_with_row(db, people, court, "C-4-2026", date(2026, 10, 1), status="active")
    _case_with_row(db, people, court, "C-5-2026", date(2026, 10, 1), dtype="observaciones_prueba_6d")

    resp = _login(client, people[role]).get(URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [i["deadline_id"] for i in body["items"]] == [new.id, old.id]
    first = body["items"][0]
    assert first == {
        "case_id": c_new.id, "rol": "C-2-2026", "tribunal": "12º Juzgado Civil",
        "abogado": "lawyer X", "deadline_id": new.id, "tipo": "excepciones_8d",
        "tipo_label": "Plazo para oponer excepciones", "due_date": "2026-10-19",
        "dias_desde": 28, "enrojece": True,
    }
    assert body["items"][1]["enrojece"] is False and body["items"][1]["dias_desde"] == 90


@pytest.mark.parametrize("role", ["lawyer", "procurador"])
def test_lawyer_and_procurador_are_forbidden(client, db, people, court, role):
    _case_with_row(db, people, court, "C-1-2026", date(2026, 10, 19))
    assert _login(client, people[role]).get(URL).status_code == 403


def test_marking_the_deadline_clears_it_from_the_list_and_the_redness(client, db, people, court):
    case, row = _case_with_row(db, people, court, "C-9-2026", date(2026, 10, 19))
    c = _login(client, people["admin"])
    resp = c.put(f"/api/v1/cases/{case.id}/deadlines/{row.id}/status", json={"status": "no_cumplido"})
    assert resp.status_code == 200
    assert c.get(URL).json()["total"] == 0
    db.refresh(case)
    assert case.semaforo_motivo is None


def test_case_detail_and_list_expose_the_reason(client, db, people, court):
    case, _ = _case_with_row(db, people, court, "C-7-2026", date(2026, 10, 19))
    case.semaforo_motivo = "Abandono disponible: presentar el escrito"
    db.commit()
    c = _login(client, people["admin"])
    assert c.get(f"/api/v1/cases/{case.id}").json()["case"]["semaforo_motivo"] == case.semaforo_motivo
    items = c.get("/api/v1/cases", params={"limit": 50}).json()["items"]
    assert items[0]["semaforo_motivo"] == case.semaforo_motivo
