"""Tests for the bonus variables + liquidación endpoints (Hitos Slice 2).

Covers admin-only access, nivel assignment, the upsert of manual inputs with its
validations, and the assembled liquidación (Fijo + Hitos aprobados + V1+V3+V2).
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo, HITO_APROBADO
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
def junior(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Fernanda Arroyo", role="lawyer", nivel="junior")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def test_requires_admin(client, junior):
    r = client.get("/api/v1/bono/liquidacion?periodo=2026-07", headers=_h(LAWYER_RUT))
    assert r.status_code == 403


def test_parametros(client, admin):
    r = client.get("/api/v1/bono/parametros", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    body = r.json()
    assert body["fijo_junior"] == 1_105_354
    assert body["fijo_pleno"] == 1_363_354
    assert body["v2_por_renovacion"] == 10_400


def test_set_nivel_and_liquidacion_includes_only_nivel_lawyers(client, admin, db):
    # a lawyer with no nivel is excluded until assigned
    lw = Lawyer(rut="11111111-1", name="Sandy Quijada", role="lawyer")
    db.add(lw)
    db.commit()
    db.refresh(lw)

    r = client.get("/api/v1/bono/liquidacion?periodo=2026-07", headers=_h(ADMIN_RUT))
    assert all(row["lawyer_id"] != lw.id for row in r.json()["rows"])

    r = client.put(f"/api/v1/bono/nivel/{lw.id}", headers=_h(ADMIN_RUT), json={"nivel": "pleno"})
    assert r.status_code == 200
    r = client.get("/api/v1/bono/liquidacion?periodo=2026-07", headers=_h(ADMIN_RUT))
    row = next(x for x in r.json()["rows"] if x["lawyer_id"] == lw.id)
    assert row["nivel"] == "pleno"
    assert row["fijo"] == 1_363_354


def test_set_nivel_rejects_bad_value(client, admin, junior):
    r = client.put(f"/api/v1/bono/nivel/{junior.id}", headers=_h(ADMIN_RUT), json={"nivel": "senior"})
    assert r.status_code == 400


def test_upsert_variables_computes_bonus(client, admin, junior):
    r = client.put(
        f"/api/v1/bono/variables/{junior.id}?periodo=2026-07",
        headers=_h(ADMIN_RUT),
        json={
            "clientes_m2": 100, "clientes_activos": 90,
            "causas_asignadas": 100, "causas_cumplidas": 95,
            "renovaciones": 2, "verificado_dj": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["v1_bruto"] == 6462 * 90
    assert body["v3_neta"] == 100_000
    assert body["v2_bruto"] == 20_800
    assert body["has_row"] is True
    assert body["verificado_dj"] is True

    # upsert is idempotent on the same period (updates, not duplicates)
    r2 = client.put(
        f"/api/v1/bono/variables/{junior.id}?periodo=2026-07",
        headers=_h(ADMIN_RUT),
        json={"clientes_m2": 100, "clientes_activos": 80},
    )
    assert r2.json()["v1_bruto"] == 6462 * 80  # 80% still ≥80% tramo
    liq = client.get("/api/v1/bono/liquidacion?periodo=2026-07", headers=_h(ADMIN_RUT)).json()
    assert sum(1 for row in liq["rows"] if row["lawyer_id"] == junior.id) == 1


def test_upsert_rejects_activos_gt_m2(client, admin, junior):
    r = client.put(
        f"/api/v1/bono/variables/{junior.id}?periodo=2026-07",
        headers=_h(ADMIN_RUT),
        json={"clientes_m2": 50, "clientes_activos": 80},
    )
    assert r.status_code == 400


def test_upsert_rejects_lawyer_without_nivel(client, admin, db):
    lw = Lawyer(rut="22222222-2", name="Sin Nivel", role="lawyer")
    db.add(lw)
    db.commit()
    db.refresh(lw)
    r = client.put(
        f"/api/v1/bono/variables/{lw.id}?periodo=2026-07",
        headers=_h(ADMIN_RUT),
        json={"clientes_m2": 10, "clientes_activos": 5},
    )
    assert r.status_code == 409


def test_liquidacion_includes_approved_hitos(client, admin, junior, db):
    tipo = HitoTipo(code="j_presc", label="Prescripción", nivel="junior", valor_bruto=8_077, orden=1)
    db.add(tipo)
    db.commit()
    db.refresh(tipo)
    h = Hito(
        lawyer_id=junior.id, hito_tipo_id=tipo.id, valor_bruto=8_077,
        fecha_hito=date(2026, 7, 15), estado=HITO_APROBADO,
        evidencia_storage_key="x",
    )
    db.add(h)
    db.commit()

    liq = client.get("/api/v1/bono/liquidacion?periodo=2026-07", headers=_h(ADMIN_RUT)).json()
    row = next(x for x in liq["rows"] if x["lawyer_id"] == junior.id)
    assert row["hitos_aprobados"] == 8_077
    assert row["total_bruto"] == row["fijo"] + 8_077 + row["total_bono_gestion"]
