"""Tests for app/api/v1/cartera.py — the cartera snapshot endpoints.

Covers role guards (admin/auditor read, admin-only write), the snapshot
list/detail/comparar/frescura response shapes, and that POST
/cartera/snapshot's response makes ``tomado_at`` explicit rather than
implying a day-1 snapshot.
"""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.cartera_snapshot import periodo_actual, tomar_snapshot

ADMIN_RUT = "11111111-1"
AUDITOR_RUT = "77777777-7"
LAWYER_RUT = "88888888-8"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def auditor(db):
    obj = Lawyer(rut=AUDITOR_RUT, name="Auditor", role="auditor")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def plain_lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Lawyer", role="lawyer", is_firm_lawyer=True, nivel="junior")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-CARTERA-API", name="Juzgado Cartera API", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _headers(rut):
    tok = create_access_token({"sub": rut}, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def admin_headers(admin):
    return _headers(ADMIN_RUT)


@pytest.fixture
def auditor_headers(auditor):
    return _headers(AUDITOR_RUT)


@pytest.fixture
def lawyer_headers(plain_lawyer):
    return _headers(LAWYER_RUT)


def _make_case(db, owner_lawyer, court, rol, **kwargs):
    obj = Case(
        lawyer_id=owner_lawyer.id,
        court_id=court.id,
        rol=rol,
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        **kwargs,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _seed_litigante(db, case, lawyer):
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut=lawyer.rut,
            persona_type="NATURAL",
            nombre=lawyer.name,
            natural_key=f"{case.id}-{lawyer.rut}",
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# POST /cartera/snapshot
# ---------------------------------------------------------------------------


def test_post_snapshot_admin_creates_and_reports_tomado_at(
    client, db, admin_headers, plain_lawyer, court
):
    case = _make_case(db, plain_lawyer, court, "C-100-2026", matriz="M2")
    _seed_litigante(db, case, plain_lawyer)

    before = datetime.utcnow()
    resp = client.post("/api/v1/cartera/snapshot", json={}, headers=admin_headers)
    after = datetime.utcnow()

    assert resp.status_code == 200
    body = resp.json()
    assert body["periodo"] == periodo_actual()
    assert body["causas"] == 1
    assert body["tomado_por"] == ADMIN_RUT

    tomado_at = datetime.fromisoformat(body["tomado_at"])
    # tomado_at is the exact moment this call ran, not a fabricated day-1
    # timestamp — must fall inside this test's own execution window.
    assert before <= tomado_at <= after


def test_post_snapshot_explicit_periodo_and_reemplazar(client, db, admin_headers, plain_lawyer, court):
    _make_case(db, plain_lawyer, court, "C-101-2026", matriz="M1 Baja")
    resp1 = client.post(
        "/api/v1/cartera/snapshot", json={"periodo": "2026-01"}, headers=admin_headers
    )
    assert resp1.status_code == 200
    assert resp1.json()["periodo"] == "2026-01"
    assert resp1.json()["causas"] == 1

    _make_case(db, plain_lawyer, court, "C-102-2026", matriz="M2")
    resp2 = client.post(
        "/api/v1/cartera/snapshot", json={"periodo": "2026-01"}, headers=admin_headers
    )
    assert resp2.json()["causas"] == 1  # idempotent, second case not picked up

    resp3 = client.post(
        "/api/v1/cartera/snapshot",
        json={"periodo": "2026-01", "reemplazar": True},
        headers=admin_headers,
    )
    assert resp3.json()["causas"] == 2


def test_post_snapshot_rejects_non_admin(client, db, auditor_headers, lawyer_headers):
    assert client.post("/api/v1/cartera/snapshot", json={}, headers=auditor_headers).status_code == 403
    assert client.post("/api/v1/cartera/snapshot", json={}, headers=lawyer_headers).status_code == 403


# ---------------------------------------------------------------------------
# GET /cartera/snapshots
# ---------------------------------------------------------------------------


def test_list_snapshots(client, db, admin_headers, auditor_headers, plain_lawyer, court):
    _make_case(db, plain_lawyer, court, "C-103-2026", matriz="M2")
    tomar_snapshot(db, "2026-02")
    tomar_snapshot(db, "2026-03")

    resp = client.get("/api/v1/cartera/snapshots", headers=auditor_headers)
    assert resp.status_code == 200
    periodos = [item["periodo"] for item in resp.json()["items"]]
    assert "2026-02" in periodos
    assert "2026-03" in periodos

    assert client.get("/api/v1/cartera/snapshots", headers=admin_headers).status_code == 200


def test_list_snapshots_rejects_plain_lawyer(client, lawyer_headers):
    assert client.get("/api/v1/cartera/snapshots", headers=lawyer_headers).status_code == 403


# ---------------------------------------------------------------------------
# GET /cartera/snapshot/{periodo}
# ---------------------------------------------------------------------------


def test_get_snapshot_detail_shape(client, db, admin_headers, auditor_headers, plain_lawyer, court):
    case = _make_case(db, plain_lawyer, court, "C-104-2026", matriz="M2")
    _seed_litigante(db, case, plain_lawyer)
    tomar_snapshot(db, "2026-04")

    resp = client.get("/api/v1/cartera/snapshot/2026-04", headers=auditor_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["periodo"] == "2026-04"
    assert body["causas"] == 1
    assert any(item["matriz"] == "M2" for item in body["por_matriz"])
    abogado_row = next(r for r in body["por_abogado"] if r["lawyer_id"] == plain_lawyer.id)
    assert abogado_row["nivel"] == "junior"
    assert abogado_row["causas"] == 1
    assert "frescura" in body
    assert "pct_al_dia" in body["frescura"]


def test_get_snapshot_detail_404_when_missing(client, admin_headers):
    resp = client.get("/api/v1/cartera/snapshot/2099-01", headers=admin_headers)
    assert resp.status_code == 404


def test_get_snapshot_detail_rejects_plain_lawyer(client, lawyer_headers):
    assert client.get("/api/v1/cartera/snapshot/2026-04", headers=lawyer_headers).status_code == 403


# ---------------------------------------------------------------------------
# GET /cartera/comparar
# ---------------------------------------------------------------------------


def test_comparar_endpoint(client, db, admin_headers, plain_lawyer, court):
    _make_case(db, plain_lawyer, court, "C-105-2026", matriz="M1 Baja")
    tomar_snapshot(db, "2026-05")
    _make_case(db, plain_lawyer, court, "C-106-2026", matriz="M2")
    tomar_snapshot(db, "2026-06")

    resp = client.get(
        "/api/v1/cartera/comparar",
        params={"desde": "2026-05", "hasta": "2026-06"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["desde"] == "2026-05"
    assert body["hasta"] == "2026-06"
    m2 = next(item for item in body["por_matriz"] if item["matriz"] == "M2")
    assert m2["desde"] == 0
    assert m2["hasta"] == 1
    assert m2["delta"] == 1


def test_comparar_rejects_plain_lawyer(client, lawyer_headers):
    resp = client.get(
        "/api/v1/cartera/comparar",
        params={"desde": "2026-05", "hasta": "2026-06"},
        headers=lawyer_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /cartera/frescura
# ---------------------------------------------------------------------------


def test_get_frescura_shape_and_advertencia(client, db, admin_headers, auditor_headers, plain_lawyer, court):
    now = datetime.utcnow()
    # 1 fresh, 3 stale -> 25% al día -> advertencia must be present.
    _make_case(db, plain_lawyer, court, "C-107-2026", last_detail_checked_at=now)
    for i in range(3):
        _make_case(
            db,
            plain_lawyer,
            court,
            f"C-10{8 + i}-2026",
            last_detail_checked_at=now - timedelta(days=200),
        )

    resp = client.get("/api/v1/cartera/frescura", headers=auditor_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["pct_al_dia"] == 25.0
    assert body["advertencia"] is not None
    assert "detalle del PJUD al día" in body["advertencia"]

    assert client.get("/api/v1/cartera/frescura", headers=admin_headers).status_code == 200


def test_get_frescura_rejects_plain_lawyer(client, lawyer_headers):
    assert client.get("/api/v1/cartera/frescura", headers=lawyer_headers).status_code == 403
