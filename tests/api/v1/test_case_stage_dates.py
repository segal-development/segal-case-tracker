"""GET /api/v1/cases/{id} -> ``stage_dates``: when each procedural stage was reached.

Computed on the fly from the movements already loaded by ``get_case``
(MovementClassifier.stage_history); nothing is stored.
"""

from datetime import datetime

import pytest
from sqlalchemy import event

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from tests.conftest import engine

NOTIF = "NOTIFICACIÓN DE DEMANDA (Exitosa)"
PRUEBA = "Notificación resolución que recibe la causa a prue (Exitosa)"


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Owner Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def outsider(db):
    obj = Lawyer(rut="22222222-2", name="Outside Lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-SD", name="Juzgado Stage Dates", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, procedure=None):
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        procedure=procedure,
        plaintiff="BANCO TEST",
        defendant="FERNANDEZ",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante="AB.DTE",
            rut=lawyer.rut,
            persona_type="NATURAL",
            nombre=lawyer.name,
            natural_key=f"{lawyer.rut}|ab.dte",
        )
    )
    db.commit()
    return case


def _add_movements(db, case, rows):
    for i, (when, stage, description) in enumerate(rows, start=1):
        db.add(
            Movement(
                case_id=case.id,
                folio=i,
                stage=stage,
                procedure="Ejecutivo",
                description=description,
                movement_date=datetime.strptime(when, "%Y-%m-%d"),
            )
        )
    db.commit()


FLOW = [
    ("2026-04-09", "Gestión Preparatoria", NOTIF),
    ("2026-04-13", "Excepciones", "Opone excepciones"),
    ("2026-05-30", "Contestación Excepciones", PRUEBA),
]


def _as(lawyer):
    async def _override():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _override


def _get(client, case):
    return client.get(f"/api/v1/cases/{case.id}")


def test_stage_dates_maps_each_reached_stage_to_iso_date(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-1-2026")
    _add_movements(db, case, FLOW)
    _as(lawyer)

    resp = _get(client, case)

    assert resp.status_code == 200
    assert resp.json()["stage_dates"] == {
        "notificado": "2026-04-09",
        "excepciones": "2026-04-13",
        "auto_prueba": "2026-05-30",
    }


def test_case_without_movements_returns_empty_stage_dates(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-2-2026")
    _as(lawyer)

    resp = _get(client, case)

    assert resp.status_code == 200
    assert resp.json()["stage_dates"] == {}


def test_medida_prejudicial_returns_empty_stage_dates(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-3-2026", procedure="Medida prejudicial")
    _add_movements(db, case, FLOW)
    _as(lawyer)

    resp = _get(client, case)

    assert resp.status_code == 200
    assert resp.json()["stage_dates"] == {}


def test_existing_response_fields_are_unchanged(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-4-2026")
    _add_movements(db, case, FLOW)
    _as(lawyer)

    body = _get(client, case).json()

    assert body["case"]["id"] == case.id
    assert body["movements_count"] == 3
    assert [m["folio"] for m in body["movements"]] == ["3", "2", "1"]


def test_lawyer_outside_scope_gets_404_and_no_dates(client, db, lawyer, outsider, court):
    case = _make_case(db, lawyer, court, "C-5-2026")
    _add_movements(db, case, FLOW)
    _as(outsider)

    resp = _get(client, case)

    assert resp.status_code == 404
    assert "stage_dates" not in resp.json()


def _count_selects(client, case):
    statements = []

    def _listener(conn, cursor, statement, *args):
        if statement.lstrip()[:6].upper() == "SELECT":
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        assert _get(client, case).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _listener)
    return len(statements)


def test_stage_dates_adds_no_queries_as_movements_grow(client, db, lawyer, court):
    small = _make_case(db, lawyer, court, "C-6-2026")
    _add_movements(db, small, FLOW[:1])
    big = _make_case(db, lawyer, court, "C-7-2026")
    _add_movements(db, big, FLOW)
    _as(lawyer)

    # The shared test session caches each case's lazy relations after its
    # first request; warm both up so only per-request queries are compared.
    _count_selects(client, small)
    _count_selects(client, big)

    assert _count_selects(client, small) == _count_selects(client, big)
