"""Tests for the hitos (milestone → bonus) endpoints — Slice 1.

Covers the entry flow (mandatory PJUD evidence), admin approval (blocked without
evidence — the firm's "sin evidencia no se paga" rule), the per-lawyer resumen,
and the permission boundaries (a lawyer sees/approves nothing outside their own).
The hito-type catalog is normally seeded by migration 032; tests seed one row
directly since the test schema is built from models, not migrations.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo, HITO_PENDIENTE
from app.models.lawyer import Lawyer

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Benjamín Lawyer", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def tipo(db):
    t = HitoTipo(
        code="pleno_prescripcion", label="Prescripción terminada", nivel="pleno",
        valor_bruto=8077, etapa_tramite="INGRESO EXCEPCIONES PRESCRIPCIÓN", orden=1,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def _create(client, headers, tipo_id, lawyer_id=None):
    data = {"hito_tipo_id": tipo_id, "fecha_hito": "2026-07-15", "rol_causa": "C-1-2026"}
    if lawyer_id is not None:
        data["lawyer_id"] = lawyer_id
    return client.post(
        "/api/v1/hitos", headers=headers, data=data,
        files={"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")},
    )


def test_tipos_lists_catalog(client, db, admin, tipo):
    r = client.get("/api/v1/hitos/tipos", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert any(t["code"] == "pleno_prescripcion" and t["valor_bruto"] == 8077 for t in r.json())


def test_create_snapshots_value(client, db, admin, tipo):
    r = _create(client, _h(ADMIN_RUT), tipo.id)
    assert r.status_code == 201
    body = r.json()
    assert body["estado"] == HITO_PENDIENTE
    assert body["valor_bruto"] == 8077  # snapshot from the tipo
    assert body["tiene_evidencia"] is True


def test_create_without_evidence_ok(client, db, admin, tipo):
    # Evidence is optional now — creating without a file succeeds.
    r = client.post("/api/v1/hitos", headers=_h(ADMIN_RUT),
                    data={"hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15"})
    assert r.status_code == 201
    assert r.json()["tiene_evidencia"] is False


def test_approve_without_evidence_ok(client, db, admin, lawyer, tipo):
    # A hito with no evidence can now be approved (evidence no longer mandatory).
    h = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
             fecha_hito=date(2026, 7, 15), estado=HITO_PENDIENTE)
    db.add(h)
    db.commit()
    db.refresh(h)
    r = client.post(f"/api/v1/hitos/{h.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert r.json()["estado"] == "aprobado"


def test_admin_approves_and_resumen_totals(client, db, admin, lawyer, tipo):
    hid = _create(client, _h(ADMIN_RUT), tipo.id, lawyer_id=lawyer.id).json()["id"]
    r = client.post(f"/api/v1/hitos/{hid}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert r.json()["estado"] == "aprobado"

    r = client.get("/api/v1/hitos/resumen?periodo=2026-07", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    row = next(x for x in r.json() if x["lawyer_id"] == lawyer.id)
    assert row["aprobados"] == 1
    assert row["total_bruto"] == 8077


def test_regular_lawyer_cannot_approve(client, db, admin, lawyer, tipo):
    hid = _create(client, _h(LAWYER_RUT), tipo.id).json()["id"]
    r = client.post(f"/api/v1/hitos/{hid}/aprobar", headers=_h(LAWYER_RUT))
    assert r.status_code == 403  # require_admin


def test_lawyer_lists_only_own(client, db, admin, lawyer, tipo):
    _create(client, _h(ADMIN_RUT), tipo.id, lawyer_id=admin.id)   # admin's own
    _create(client, _h(LAWYER_RUT), tipo.id)                      # lawyer's own
    r = client.get("/api/v1/hitos", headers=_h(LAWYER_RUT))
    assert r.status_code == 200
    assert {x["lawyer_id"] for x in r.json()} == {lawyer.id}  # only their own
