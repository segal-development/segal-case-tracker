"""Tests for the auditor role — deadline audit and manual deadline management."""

import pytest
from datetime import date, timedelta

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.alert import Alert

AUDITOR_RUT = "44444444-4"
LAWYER_RUT = "55555555-5"
ADMIN_RUT = "66666666-6"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auditor(db):
    l = Lawyer(rut=AUDITOR_RUT, name="Auditor User", role="auditor")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def owning_lawyer(db):
    l = Lawyer(rut=LAWYER_RUT, name="Case Lawyer", role="lawyer")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def admin_lawyer(db):
    l = Lawyer(rut=ADMIN_RUT, name="Admin User", role="admin")
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


@pytest.fixture
def auditor_headers(auditor):
    tok = create_access_token({"sub": AUDITOR_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def lawyer_headers(owning_lawyer):
    tok = create_access_token({"sub": LAWYER_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def admin_headers(admin_lawyer):
    tok = create_access_token({"sub": ADMIN_RUT}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def case_with_deadline(db, owning_lawyer):
    """A case owned by owning_lawyer with one active deadline."""
    # court_id=1 is a dummy value; SQLite in conftest does not enforce FK constraints.
    case = Case(rol="C-100-2026", lawyer_id=owning_lawyer.id, court_id=1)
    db.add(case)
    db.commit()
    db.refresh(case)

    dl = CaseDeadline(
        case_id=case.id,
        deadline_type="excepciones_8d",
        legal_basis="art. 459 CPC",
        due_date=date.today() + timedelta(days=5),
        triggered_at=date.today(),
        status="active",
    )
    db.add(dl)
    db.commit()
    db.refresh(dl)
    return case, dl


# ---------------------------------------------------------------------------
# require_auditor gate
# ---------------------------------------------------------------------------


def test_put_status_requires_auditor_role_403(client, case_with_deadline, lawyer_headers):
    case, dl = case_with_deadline
    resp = client.put(
        f"/api/v1/cases/{case.id}/deadlines/{dl.id}/status",
        json={"status": "cumplido"},
        headers=lawyer_headers,
    )
    assert resp.status_code == 403


def test_post_manual_deadline_requires_auditor_role_403(client, case_with_deadline, lawyer_headers):
    case, dl = case_with_deadline
    resp = client.post(
        f"/api/v1/cases/{case.id}/deadlines",
        json={"deadline_type": "apelacion_5d", "due_date": str(date.today() + timedelta(days=3))},
        headers=lawyer_headers,
    )
    assert resp.status_code == 403


def test_put_status_auditor_allowed(client, case_with_deadline, auditor, auditor_headers):
    case, dl = case_with_deadline
    resp = client.put(
        f"/api/v1/cases/{case.id}/deadlines/{dl.id}/status",
        json={"status": "cumplido"},
        headers=auditor_headers,
    )
    assert resp.status_code == 200


def test_put_status_admin_allowed(client, case_with_deadline, admin_lawyer, admin_headers):
    case, dl = case_with_deadline
    resp = client.put(
        f"/api/v1/cases/{case.id}/deadlines/{dl.id}/status",
        json={"status": "cumplido"},
        headers=admin_headers,
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT /{case_id}/deadlines/{deadline_id}/status
# ---------------------------------------------------------------------------


def test_put_status_sets_fields_and_creates_alert(client, db, case_with_deadline, auditor, auditor_headers):
    case, dl = case_with_deadline
    resp = client.put(
        f"/api/v1/cases/{case.id}/deadlines/{dl.id}/status",
        json={"status": "cumplido"},
        headers=auditor_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cumplido"
    assert data["marked_by"] == auditor.id
    assert data["marked_at"] is not None

    # Alert created for case's owning lawyer
    db.expire_all()
    alert = db.query(Alert).filter(
        Alert.case_id == case.id,
        Alert.type == "deadline_audit",
    ).first()
    assert alert is not None
    assert alert.lawyer_id == case.lawyer_id


def test_put_status_invalid_status_422(client, case_with_deadline, auditor, auditor_headers):
    case, dl = case_with_deadline
    resp = client.put(
        f"/api/v1/cases/{case.id}/deadlines/{dl.id}/status",
        json={"status": "invalid_status"},
        headers=auditor_headers,
    )
    assert resp.status_code == 422


def test_put_status_404_wrong_case(client, db, auditor, auditor_headers, owning_lawyer):
    other_case = Case(rol="C-999-2026", lawyer_id=owning_lawyer.id, court_id=1)
    db.add(other_case)
    db.commit()
    db.refresh(other_case)
    dl = CaseDeadline(
        case_id=other_case.id,
        deadline_type="apelacion_5d",
        legal_basis="art. 187/475 CPC",
        due_date=date.today() + timedelta(days=3),
        triggered_at=date.today(),
        status="active",
    )
    db.add(dl)
    db.commit()
    db.refresh(dl)
    # Try to update dl using a non-existent case_id
    resp = client.put(
        f"/api/v1/cases/99999/deadlines/{dl.id}/status",
        json={"status": "cumplido"},
        headers=auditor_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /{case_id}/deadlines — manual deadline creation
# ---------------------------------------------------------------------------


def test_post_manual_deadline_creates_is_manual_true(client, db, case_with_deadline, auditor, auditor_headers):
    case, _ = case_with_deadline
    due = str(date.today() + timedelta(days=10))
    resp = client.post(
        f"/api/v1/cases/{case.id}/deadlines",
        json={"deadline_type": "apelacion_5d", "due_date": due},
        headers=auditor_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["is_manual"] is True
    assert data["deadline_type"] == "apelacion_5d"
    assert data["legal_basis"] == "art. 187/475 CPC"  # from catalog

    # Alert created for case's owning lawyer
    db.expire_all()
    alert = db.query(Alert).filter(
        Alert.case_id == case.id,
        Alert.type == "deadline_added",
    ).first()
    assert alert is not None
    assert alert.lawyer_id == case.lawyer_id


def test_post_manual_deadline_invalid_type_422(client, case_with_deadline, auditor, auditor_headers):
    case, _ = case_with_deadline
    resp = client.post(
        f"/api/v1/cases/{case.id}/deadlines",
        json={"deadline_type": "not_a_real_type", "due_date": str(date.today())},
        headers=auditor_headers,
    )
    assert resp.status_code == 422


def test_post_manual_deadline_case_not_found_404(client, auditor, auditor_headers):
    resp = client.post(
        "/api/v1/cases/99999/deadlines",
        json={"deadline_type": "apelacion_5d", "due_date": str(date.today() + timedelta(days=5))},
        headers=auditor_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /deadlines/catalog
# ---------------------------------------------------------------------------


def test_get_catalog_returns_all_types(client, owning_lawyer, lawyer_headers):
    from app.core.deadlines_config import DeadlineType

    resp = client.get("/api/v1/cases/deadlines/catalog", headers=lawyer_headers)
    assert resp.status_code == 200
    data = resp.json()
    values = {item["value"] for item in data}
    assert values == {dt.value for dt in DeadlineType}
    # Each item has required fields
    item = data[0]
    assert "value" in item
    assert "label" in item
    assert "dias" in item
    assert "legal_basis" in item
    assert "is_fatal" in item


def test_get_catalog_dias_field_matches_config(client, owning_lawyer, lawyer_headers):
    """dias in response should match dias_habiles from DeadlineType."""
    from app.core.deadlines_config import DeadlineType

    resp = client.get("/api/v1/cases/deadlines/catalog", headers=lawyer_headers)
    assert resp.status_code == 200
    data = resp.json()
    catalog_map = {item["value"]: item for item in data}
    for dt in DeadlineType:
        assert catalog_map[dt.value]["dias"] == dt.dias_habiles
        assert catalog_map[dt.value]["is_fatal"] == dt.is_fatal


# ---------------------------------------------------------------------------
# Engine guard: manual + cumplido rows not superseded
# ---------------------------------------------------------------------------


def test_recompute_does_not_supersede_manual_deadline(db, owning_lawyer):
    """recompute_case must not touch is_manual=True rows."""
    from app.services.deadline_engine import DeadlineEngine

    # court_id=1 dummy; FK not enforced in conftest SQLite
    case = Case(rol="E-200-2026", lawyer_id=owning_lawyer.id, court_id=1)
    db.add(case)
    db.commit()
    db.refresh(case)

    manual_dl = CaseDeadline(
        case_id=case.id,
        deadline_type="apelacion_5d",
        legal_basis="art. 187/475 CPC",
        due_date=date.today() + timedelta(days=10),
        triggered_at=date.today(),
        status="active",
        is_manual=True,
    )
    db.add(manual_dl)
    db.commit()
    db.refresh(manual_dl)

    # Run recompute (no movements → no new triggers)
    DeadlineEngine.recompute_case(db, case)
    db.expire_all()

    db.refresh(manual_dl)
    assert manual_dl.status == "active", "Manual deadline must not be superseded by the engine"


def test_recompute_does_not_touch_cumplido_deadline(db, owning_lawyer):
    """recompute_case must not change status of a cumplido row."""
    from app.services.deadline_engine import DeadlineEngine

    case = Case(rol="E-201-2026", lawyer_id=owning_lawyer.id, court_id=1)
    db.add(case)
    db.commit()
    db.refresh(case)

    audited_dl = CaseDeadline(
        case_id=case.id,
        deadline_type="apelacion_5d",
        legal_basis="art. 187/475 CPC",
        due_date=date.today() + timedelta(days=3),
        triggered_at=date.today(),
        status="cumplido",
        is_manual=False,
    )
    db.add(audited_dl)
    db.commit()
    db.refresh(audited_dl)

    DeadlineEngine.recompute_case(db, case)
    db.expire_all()

    db.refresh(audited_dl)
    assert audited_dl.status == "cumplido", "Cumplido deadline must not be touched by the engine"


# ---------------------------------------------------------------------------
# lawyers PUT accepts auditor role
# ---------------------------------------------------------------------------


def test_put_account_accepts_auditor_role(client, admin_lawyer, admin_headers, owning_lawyer):
    resp = client.put(
        f"/api/v1/lawyers/{owning_lawyer.id}/account",
        json={"role": "auditor"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "auditor"
