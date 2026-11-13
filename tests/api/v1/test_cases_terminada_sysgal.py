"""S10: terminada_en_sysgal in list/detail, ?terminada_sysgal filter, summary count, ?matriz exclusion."""

from datetime import datetime

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Lawyer TS", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other(db):
    obj = Lawyer(rut="22222222-2", name="Other TS", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-TS", name="Juzgado TS", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, lawyer):
    async def _mock():
        return {"sub": str(lawyer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


def _case(db, owner, court, rol, **fields) -> Case:
    obj = Case(lawyer_id=owner.id, court_id=court.id, rol=rol, status="active", competencia="civil",
               created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **fields)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _ids(resp):
    return {row["id"] for row in resp.json()["items"]}


def test_list_items_and_detail_expose_the_flag(authed_client, db, lawyer, court):
    term = _case(db, lawyer, court, "C-1-2026", terminada_en_sysgal=True)
    reg = _case(db, lawyer, court, "C-2-2026")

    items = {r["id"]: r for r in authed_client.get("/api/v1/cases").json()["items"]}
    assert items[term.id]["terminada_en_sysgal"] is True
    assert items[reg.id]["terminada_en_sysgal"] is False
    assert authed_client.get(f"/api/v1/cases/{term.id}").json()["case"]["terminada_en_sysgal"] is True
    assert authed_client.get(f"/api/v1/cases/{reg.id}").json()["case"]["terminada_en_sysgal"] is False


def test_filter_true_false_and_absent(authed_client, db, lawyer, court):
    term = _case(db, lawyer, court, "C-1-2026", terminada_en_sysgal=True)
    reg = _case(db, lawyer, court, "C-2-2026")

    assert _ids(authed_client.get("/api/v1/cases", params={"terminada_sysgal": True})) == {term.id}
    assert _ids(authed_client.get("/api/v1/cases", params={"terminada_sysgal": False})) == {reg.id}
    assert _ids(authed_client.get("/api/v1/cases")) == {term.id, reg.id}


def test_summary_counts_within_scope_and_total_is_unchanged(authed_client, db, lawyer, other, court):
    _case(db, lawyer, court, "C-1-2026", terminada_en_sysgal=True)
    _case(db, lawyer, court, "C-2-2026")
    _case(db, other, court, "C-3-2026", terminada_en_sysgal=True)  # not visible to `lawyer`

    body = authed_client.get("/api/v1/cases/summary").json()

    assert body["terminada_sysgal"] == 1
    assert body["total"] == 2


def test_matriz_filter_excludes_terminadas_and_equals_the_chip(authed_client, db, lawyer, court):
    live = _case(db, lawyer, court, "C-1-2026", matriz="M2")
    _case(db, lawyer, court, "C-2-2026", matriz="M2", terminada_en_sysgal=True)

    resp = authed_client.get("/api/v1/cases", params={"matriz": "M2"})

    assert _ids(resp) == {live.id}
    assert resp.json()["total"] == 1


def test_terminada_filter_ignores_the_matriz_exclusion_when_asked_explicitly(authed_client, db, lawyer, court):
    term = _case(db, lawyer, court, "C-2-2026", matriz="M2", terminada_en_sysgal=True)

    # matriz + terminada_sysgal=true together: the explicit request wins, so the chip
    # "Terminada en Sysgal" can still be combined with other filters.
    resp = authed_client.get("/api/v1/cases", params={"matriz": "M2", "terminada_sysgal": True})

    assert _ids(resp) == {term.id}
