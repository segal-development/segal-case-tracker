"""``abogado_id`` / ``abogado_nombre``: the lawyer who works the causa.

The detail screen must show the CASE's lawyer, not the viewing admin. The
payload therefore carries the effective lawyer (``Case.effective_lawyer_id``).
"""

from datetime import datetime

import pytest

from app.api.deps import get_current_lawyer
from app.main import app
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


@pytest.fixture
def viewer(db):
    obj = Lawyer(rut="11111111-1", name="Viewer Admin", role="admin")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def owner(db):
    obj = Lawyer(rut="22222222-2", name="Owner Lawyer", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def delegate(db):
    obj = Lawyer(rut="33333333-3", name="Delegate Lawyer", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-AB", name="Juzgado AB", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def authed_client(client, viewer):
    async def _mock():
        return {"sub": str(viewer.id)}

    app.dependency_overrides[get_current_lawyer] = _mock
    yield client


def _case(db, lawyer, court, rol, **extra) -> Case:
    obj = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active",
        competencia="civil", created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(), **extra,
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def test_detail_exposes_the_case_lawyer_not_the_viewer(authed_client, db, viewer, owner, court):
    case = _case(db, owner, court, "C-1-2026")

    body = authed_client.get(f"/api/v1/cases/{case.id}").json()["case"]

    assert body["abogado_id"] == owner.id
    assert body["abogado_nombre"] == "Owner Lawyer"


def test_detail_prefers_the_assigned_lawyer_over_the_provenance_one(
    authed_client, db, owner, delegate, court
):
    case = _case(db, owner, court, "C-2-2026", assigned_lawyer_id=delegate.id)

    body = authed_client.get(f"/api/v1/cases/{case.id}").json()["case"]

    assert body["abogado_id"] == delegate.id
    assert body["abogado_nombre"] == "Delegate Lawyer"


def test_list_rows_expose_the_effective_lawyer(authed_client, db, owner, delegate, court):
    plain = _case(db, owner, court, "C-3-2026")
    moved = _case(db, owner, court, "C-4-2026", assigned_lawyer_id=delegate.id)

    items = authed_client.get("/api/v1/cases", params={"limit": 50}).json()["items"]

    by_id = {row["id"]: row for row in items}
    assert by_id[plain.id]["abogado_nombre"] == "Owner Lawyer"
    assert by_id[moved.id]["abogado_id"] == delegate.id
    assert by_id[moved.id]["abogado_nombre"] == "Delegate Lawyer"
