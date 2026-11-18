"""estado_pago on the Semáforo panel rows and the case detail (S9). Display only."""

from datetime import date, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_contrato import SysgalContrato
from app.services.business_days import add_business_days

PANEL = "/api/v1/cases/deadlines/semaforo"
TRIGGER = date(2026, 11, 2)
DAY3 = date(2026, 11, 5)
RUT = "16021492-9"
COPIA = datetime(2026, 11, 18, 7, 0)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.api.v1.deadlines._today_chile", lambda: DAY3)


@pytest.fixture
def world(db):
    admin = Lawyer(rut=RUT, name="Carla", email="c@segal.cl", role="admin", is_active=True)
    court = Court(code="EP-1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add_all([admin, court])
    db.commit()

    def make(rol):
        c = Case(lawyer_id=admin.id, court_id=court.id, rol=rol, competencia="civil",
                 plaintiff="Banco", defendant="Deudor", status="active")
        db.add(c)
        db.commit()
        db.add(CaseDeadline(case_id=c.id, deadline_type="excepciones_8d", triggered_at=TRIGGER,
                            due_date=add_business_days(TRIGGER, 8), status="active"))
        db.commit()
        return c

    def causa(sid, case):
        db.add(SysgalCausa(sysgal_id=sid, rol="r", rol_norm="r", tribunal="t", case_id=case.id,
                           synced_at=datetime(2026, 11, 1)))
        db.commit()

    return {"make": make, "causa": causa, "db": db}


def _headers():
    tok = create_access_token({"sub": RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


def _contrato(db, n, causas, cuota, moroso):
    db.add(SysgalContrato(contrato=str(n), cuota_actual=cuota, contrato_moroso=moroso,
                          contrato_al_dia=not moroso, id_causas=causas, copia_at=COPIA,
                          synced_at=datetime(2026, 11, 18, 9)))
    db.commit()


def test_panel_rows_and_case_detail_carry_estado_pago(client, world):
    moroso = world["make"]("C-1-2026")
    sin_info = world["make"]("C-2-2026")
    sin_causa = world["make"]("C-3-2026")
    world["causa"](10, moroso)
    world["causa"](20, sin_info)
    _contrato(world["db"], 1, [10], 2, True)

    resp = client.get(PANEL, headers=_headers())
    assert resp.status_code == 200, resp.text
    rows = {r["case_id"]: r for r in resp.json()["por_vencer"]}
    assert rows[moroso.id]["estado_pago"] == {
        "estado": "moroso", "cuota_actual": 2, "bloquearia_poder": True,
        "copia_at": "2026-11-18T07:00:00",
    }
    assert rows[sin_info.id]["estado_pago"] == {
        "estado": "sin_informacion", "cuota_actual": None, "bloquearia_poder": False, "copia_at": None,
    }
    assert rows[sin_causa.id]["estado_pago"] is None

    detail = client.get(f"/api/v1/cases/{moroso.id}", headers=_headers())
    assert detail.status_code == 200, detail.text
    assert detail.json()["estado_pago"] == rows[moroso.id]["estado_pago"]
    assert client.get(f"/api/v1/cases/{sin_causa.id}", headers=_headers()).json()["estado_pago"] is None
    assert client.get(f"/api/v1/cases/{sin_info.id}", headers=_headers()).json()["estado_pago"]["estado"] == "sin_informacion"
