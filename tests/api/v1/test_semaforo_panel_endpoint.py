"""GET /cases/deadlines/semaforo -- the fatal-plazo panel for the home screen.

Calendar: trigger Monday 2026-11-02. Day 3 = Thu 11-05 (amarillo on the 4-day
reading), day 8 = Wed 11-11.
"""

from datetime import date, timedelta

import pytest

from app.config import settings
from app.core.security import create_access_token
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.business_days import add_business_days

URL = "/api/v1/cases/deadlines/semaforo"
TRIGGER = date(2026, 11, 2)
DAY3 = date(2026, 11, 5)
RUT = "16021492-9"
OTHER_RUT = "33333333-3"
ADMIN_RUT = "22222222-2"


def _headers(rut: str) -> dict:
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.api.v1.deadlines._today_chile", lambda: DAY3)


@pytest.fixture
def people(db):
    ana = Lawyer(rut=RUT, name="Ana Pérez", email="ana@segal.cl", role="lawyer", is_active=True)
    otro = Lawyer(rut=OTHER_RUT, name="Otro Abogado", email="o@segal.cl", role="lawyer", is_active=True)
    admin = Lawyer(rut=ADMIN_RUT, name="Carla Lavín", email="c@segal.cl", role="admin", is_active=True)
    court = Court(code="SF-1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add_all([ana, otro, admin, court])
    db.commit()
    return {"ana": ana, "otro": otro, "admin": admin, "court": court}


def make_case(db, people, owner="ana", rol="C-1-2026", status="active"):
    case = Case(
        lawyer_id=people[owner].id, court_id=people["court"].id, rol=rol,
        competencia="civil", plaintiff="Banco X", defendant="Deudor", status=status,
    )
    db.add(case)
    db.commit()
    return case


def make_live(db, case, type_="excepciones_8d", triggered=TRIGGER, **kw):
    days = {"excepciones_8d": 8, "acreditar_poder_3d": 3, "apelacion_5d": 5}[type_]
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, triggered_at=triggered,
        due_date=add_business_days(triggered, days), status="active", **kw,
    )
    db.add(row)
    db.commit()
    return row


def make_expired(db, case, type_="excepciones_8d", due=date(2026, 10, 20), verdict="no_cumplido"):
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, triggered_at=due - timedelta(days=13),
        due_date=due, status="superseded", verdict=verdict,
    )
    db.add(row)
    db.commit()
    return row


def get(client, rut=RUT):
    resp = client.get(URL, headers=_headers(rut))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_requires_authentication(client) -> None:
    assert client.get(URL).status_code in (401, 403)


def test_live_excepciones_row_carries_level_both_readings_and_case_data(client, db, people) -> None:
    case = make_case(db, people)
    make_live(db, case)

    body = get(client)

    assert body["vencidos"] == []
    (row,) = body["por_vencer"]
    assert row["case_id"] == case.id
    assert row["rol"] == "C-1-2026"
    assert row["caratula"] == "Banco X/Deudor"
    assert row["tribunal"] == "1º Juzgado Civil"
    assert row["abogado_id"] == people["ana"].id
    assert row["abogado_nombre"] == "Ana Pérez"
    assert row["deadline_type"] == "excepciones_8d"
    assert row["label"] == "Plazo para oponer excepciones"
    assert row["level"] == "amarillo"
    assert row["term_confirmed"] is False
    assert [(r["term_days"], r["due_date"]) for r in row["readings"]] == [
        (4, "2026-11-06"), (8, "2026-11-11"),
    ]
    assert row["dias_habiles_remaining"] >= 0


def test_poder_is_confirmed_single_reading(client, db, people) -> None:
    case = make_case(db, people)
    # Poder ownership needs the movement heuristic; assess alone drives the level.
    make_live(db, case, "acreditar_poder_3d", triggered=TRIGGER)
    from unittest.mock import patch

    with patch("app.services.sync_service.poder_obligation_is_ours", return_value=object()):
        body = get(client)

    (row,) = body["por_vencer"]
    assert row["deadline_type"] == "acreditar_poder_3d"
    assert row["term_confirmed"] is True
    assert len(row["readings"]) == 1


def test_row_whose_assess_level_is_none_is_excluded(client, db, people) -> None:
    case = make_case(db, people)
    # Both readings are over, yet the stored due_date is still in the future.
    make_live(db, case, triggered=date(2026, 10, 1)).due_date = DAY3 + timedelta(days=5)
    db.commit()

    assert get(client)["por_vencer"] == []


def test_apelacion_is_painted_with_its_single_confirmed_term(client, db, people) -> None:
    make_live(db, make_case(db, people), "apelacion_5d")

    rows = get(client)["por_vencer"]

    assert len(rows) == 1
    assert rows[0]["deadline_type"] == "apelacion_5d"
    assert rows[0]["level"] == "amarillo"  # day 3 of 5
    assert rows[0]["term_confirmed"] is True
    assert [r["term_days"] for r in rows[0]["readings"]] == [5]


def test_counterparty_plazo_is_not_returned(client, db, people) -> None:
    case = make_case(db, people)
    db.add(CaseLitigante(
        case_id=case.id, participante="AB.DTE", rut=RUT,
        persona_type="NATURAL", nombre="Ana", natural_key="k1",
    ))
    db.commit()
    make_live(db, case)
    make_expired(db, case)

    body = get(client)

    assert body["por_vencer"] == [] and body["vencidos"] == []


def test_archived_case_or_inactive_deadline_excluded(client, db, people) -> None:
    make_live(db, make_case(db, people, rol="C-2-2026", status="archived"))
    done = make_live(db, make_case(db, people, rol="C-3-2026"))
    done.status = "cumplido"
    db.commit()

    assert get(client)["por_vencer"] == []


def test_vencidos_come_from_expired_unfiled_within_window(client, db, people, monkeypatch) -> None:
    case = make_case(db, people)
    recent = make_expired(db, case, due=date(2026, 10, 20))
    make_expired(db, case, due=date(2026, 9, 1))  # outside the 30 day window
    make_expired(db, case, due=date(2026, 10, 21), verdict="cumplido")  # we did act

    body = get(client)

    assert [r["deadline_id"] for r in body["vencidos"]] == [recent.id]
    row = body["vencidos"][0]
    assert row["level"] is None
    assert row["dias_habiles_remaining"] < 0
    assert row["rol"] == "C-1-2026"
    assert body["expired_window_days"] == 30


def test_window_is_the_existing_setting(client, db, people, monkeypatch) -> None:
    case = make_case(db, people)
    make_expired(db, case, due=date(2026, 9, 1))
    monkeypatch.setattr(settings, "SEMAFORO_EXPIRED_WINDOW_DAYS", 0)

    body = get(client)

    assert len(body["vencidos"]) == 1
    assert body["expired_window_days"] == 0


def test_expired_poder_is_not_returned(client, db, people) -> None:
    make_expired(db, make_case(db, people), "acreditar_poder_3d")

    assert get(client)["vencidos"] == []


def test_lawyer_sees_own_cases_admin_sees_firm(client, db, people) -> None:
    make_live(db, make_case(db, people, "ana", "C-1-2026"))
    make_live(db, make_case(db, people, "otro", "C-9-2026"))

    assert [r["rol"] for r in get(client, RUT)["por_vencer"]] == ["C-1-2026"]
    assert sorted(r["rol"] for r in get(client, ADMIN_RUT)["por_vencer"]) == [
        "C-1-2026", "C-9-2026",
    ]


def test_por_vencer_is_ordered_most_urgent_first(client, db, people) -> None:
    calm = make_live(db, make_case(db, people, rol="C-1-2026"), triggered=DAY3)
    hot = make_live(db, make_case(db, people, rol="C-2-2026"), triggered=date(2026, 10, 29))

    ids = [r["deadline_id"] for r in get(client)["por_vencer"]]

    assert ids.index(hot.id) < ids.index(calm.id)


def test_endpoint_is_read_only(client, db, people) -> None:
    case = make_case(db, people)
    make_live(db, case)
    make_expired(db, case)

    get(client)

    assert db.query(Alert).count() == 0
