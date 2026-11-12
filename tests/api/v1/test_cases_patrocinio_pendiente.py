"""S18: patrocinio_pendiente in list/detail, list filter and summary count (scope-aware)."""

from datetime import datetime

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Lawyer PP", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other(db):
    obj = Lawyer(rut="22222222-2", name="Other PP", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-PP", name="Juzgado PP", region="RM", type="civil")
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
    pend = _case(db, lawyer, court, "C-1-2026", patrocinio_pendiente=True)
    reg = _case(db, lawyer, court, "C-2-2026")

    items = {r["id"]: r for r in authed_client.get("/api/v1/cases").json()["items"]}
    assert items[pend.id]["patrocinio_pendiente"] is True
    assert items[reg.id]["patrocinio_pendiente"] is False
    assert authed_client.get(f"/api/v1/cases/{pend.id}").json()["case"]["patrocinio_pendiente"] is True
    assert authed_client.get(f"/api/v1/cases/{reg.id}").json()["case"]["patrocinio_pendiente"] is False


def test_filter_true_false_and_absent(authed_client, db, lawyer, court):
    pend = _case(db, lawyer, court, "C-1-2026", patrocinio_pendiente=True)
    reg = _case(db, lawyer, court, "C-2-2026")

    assert _ids(authed_client.get("/api/v1/cases", params={"patrocinio_pendiente": True})) == {pend.id}
    assert _ids(authed_client.get("/api/v1/cases", params={"patrocinio_pendiente": False})) == {reg.id}
    assert _ids(authed_client.get("/api/v1/cases")) == {pend.id, reg.id}


def test_summary_counts_pending_within_scope(authed_client, db, lawyer, other, court):
    _case(db, lawyer, court, "C-1-2026", patrocinio_pendiente=True)
    _case(db, lawyer, court, "C-2-2026")
    _case(db, other, court, "C-3-2026", patrocinio_pendiente=True)  # not visible to `lawyer`

    body = authed_client.get("/api/v1/cases/summary").json()

    assert body["patrocinio_pendiente"] == 1
    assert body["total"] == 2  # the new chip does not change the total
