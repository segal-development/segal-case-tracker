"""GET /api/v1/cases/{id} -> ``window_resolutions``: court acts of the excepciones window.

Computed on the fly from the movements already loaded by ``get_case``; nothing is stored.
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

MERO = "Mero trámite"


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
    obj = Court(code="T1-WR", name="Juzgado Window Resolutions", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _make_case(db, lawyer, court, rol, procedure=None, state="excepciones"):
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        procedure=procedure,
        procedural_state=state,
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


def _add(db, case, rows):
    for i, (when, procedure, description) in enumerate(rows, start=1):
        db.add(
            Movement(
                case_id=case.id,
                folio=i,
                stage="Excepciones",
                procedure=procedure,
                description=description,
                movement_date=datetime.strptime(when, "%Y-%m-%d"),
            )
        )
    db.commit()


WINDOW = [
    ("2026-09-21", "Escrito", "Opone excepciones"),
    ("2026-10-01", "Resolución", MERO),
    ("2026-10-05", "Escrito", "Acredita Poder"),
]


def _as(lawyer):
    async def _override():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _override


def _get(client, case):
    return client.get(f"/api/v1/cases/{case.id}")


def test_window_resolutions_listed_while_in_the_window(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-1-2026")
    _add(db, case, WINDOW)
    _as(lawyer)

    resp = _get(client, case)

    assert resp.status_code == 200
    assert resp.json()["window_resolutions"] == [
        {
            "date": "2026-10-01",
            "description": MERO,
            "requires_action": True,
            "status": "cumplida",
            "due_date": "2026-10-05",
            "resolved_on": "2026-10-05",
            "resolved_by": "Acredita Poder",
        }
    ]


def test_notificado_state_also_lists(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-2-2026", state="notificado")
    _add(db, case, WINDOW)
    _as(lawyer)

    assert len(_get(client, case).json()["window_resolutions"]) == 1


@pytest.mark.parametrize("state", ["auto_prueba", None])
def test_outside_the_window_is_empty(client, db, lawyer, court, state):
    case = _make_case(db, lawyer, court, "C-3-2026", state=state)
    _add(db, case, WINDOW)
    _as(lawyer)

    assert _get(client, case).json()["window_resolutions"] == []


def test_medida_prejudicial_is_empty(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-4-2026", procedure="Medida prejudicial")
    _add(db, case, WINDOW)
    _as(lawyer)

    assert _get(client, case).json()["window_resolutions"] == []


def test_without_opone_excepciones_is_empty(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-5-2026")
    _add(db, case, WINDOW[1:])
    _as(lawyer)

    assert _get(client, case).json()["window_resolutions"] == []


def test_existing_response_fields_are_unchanged(client, db, lawyer, court):
    case = _make_case(db, lawyer, court, "C-6-2026")
    _add(db, case, WINDOW)
    _as(lawyer)

    body = _get(client, case).json()

    assert body["case"]["id"] == case.id
    assert body["movements_count"] == 3
    assert [m["folio"] for m in body["movements"]] == ["3", "2", "1"]


def test_lawyer_outside_scope_gets_404(client, db, lawyer, outsider, court):
    case = _make_case(db, lawyer, court, "C-7-2026")
    _add(db, case, WINDOW)
    _as(outsider)

    resp = _get(client, case)

    assert resp.status_code == 404
    assert "window_resolutions" not in resp.json()


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


def test_window_resolutions_adds_no_queries_as_movements_grow(client, db, lawyer, court):
    small = _make_case(db, lawyer, court, "C-8-2026")
    _add(db, small, WINDOW[:1])
    big = _make_case(db, lawyer, court, "C-9-2026")
    _add(db, big, WINDOW)
    _as(lawyer)

    # Warm both up so only per-request queries are compared.
    _count_selects(client, small)
    _count_selects(client, big)

    assert _count_selects(client, small) == _count_selects(client, big)
