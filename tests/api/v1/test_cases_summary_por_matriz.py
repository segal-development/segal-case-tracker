"""S13: /cases/summary `por_matriz` equals the `/cases?matriz=X` total (same base)."""

from datetime import datetime

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer

MATRICES = ["M1 Baja", "M1 Alta", "M2", "M3"]


@pytest.fixture
def court(db):
    obj = Court(code="T1-PM", name="Juzgado PM", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _lawyer(db, rut, role):
    obj = Lawyer(rut=rut, name=f"L {rut}", role=role)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    return _lawyer(db, "11111111-1", "lawyer")


@pytest.fixture
def other(db):
    return _lawyer(db, "22222222-2", "lawyer")


@pytest.fixture
def admin(db):
    return _lawyer(db, "33333333-3", "admin")


def _login(client, who):
    async def _mock():
        return {"sub": str(who.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    return client


def _case(db, owner, court, rol, **fields):
    data = {"status": "active", "competencia": "civil"}
    data.update(fields)
    obj = Case(lawyer_id=owner.id, court_id=court.id, rol=rol,
               created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **data)
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def populated(db, lawyer, other, court):
    n = 0

    def add(owner, matriz, **kw):
        nonlocal n
        n += 1
        _case(db, owner, court, f"C-{n}-2026", matriz=matriz, **kw)

    for matriz, mine, theirs in [("M1 Baja", 3, 1), ("M1 Alta", 2, 2), ("M2", 4, 1), ("M3", 1, 2)]:
        for _ in range(mine):
            add(lawyer, matriz)
        for _ in range(theirs):
            add(other, matriz)
        add(lawyer, matriz, terminada_en_sysgal=True)  # excluded from the matriz figures
        add(other, matriz, terminada_en_sysgal=True)
        add(lawyer, matriz, competencia="laboral")  # outside the civil list base
        add(lawyer, matriz, status="closed")  # outside the active list base
        _case(db, lawyer, court, f"C-{n}-2010", matriz=matriz)  # below the year floor
    add(lawyer, None)  # no matriz at all


@pytest.mark.parametrize("who_fixture", ["lawyer", "admin"])
@pytest.mark.parametrize("params", [{}, {"competencia": "civil", "status": "active"}])
def test_summary_por_matriz_equals_list_total(client, populated, request, who_fixture, params):
    _login(client, request.getfixturevalue(who_fixture))

    summary = client.get("/api/v1/cases/summary", params=params).json()

    assert set(summary["por_matriz"]) == set(MATRICES)
    for matriz in MATRICES:
        listed = client.get("/api/v1/cases", params={**params, "matriz": matriz}).json()["total"]
        assert listed > 0
        assert summary["por_matriz"][matriz] == listed


def test_summary_por_matriz_values_and_other_counts_unchanged(client, populated, lawyer):
    _login(client, lawyer)

    plain = client.get("/api/v1/cases/summary", params={"competencia": "civil", "status": "active"}).json()

    assert plain["por_matriz"] == {"M1 Baja": 3, "M1 Alta": 2, "M2": 4, "M3": 1}
    # total still counts terminadas and null-matriz cases (own visible, civil+active, above floor).
    assert plain["total"] == 10 + 4 + 1
    assert plain["terminada_sysgal"] == 4


def test_summary_por_matriz_is_zero_filled_when_empty(client, lawyer):
    _login(client, lawyer)

    body = client.get("/api/v1/cases/summary").json()

    assert body["por_matriz"] == {"M1 Baja": 0, "M1 Alta": 0, "M2": 0, "M3": 0}
    assert body["total"] == 0
