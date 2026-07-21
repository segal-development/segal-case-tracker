"""Tests for the renovaciones (contract renewals) module.

Covers the derived fields (fecha_hasta = +1 year, total = cuota × 12), the RUT
normalization, the firm-lawyer selector, the monthly resumen, and delete perms.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
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
def abogado(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Eduardo Venegas", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


def test_abogados_selector_only_firm_active(client, admin, abogado, db):
    ext = Lawyer(rut="99999999-9", name="Contraparte Externa", role="lawyer", is_firm_lawyer=False)
    inact = Lawyer(rut="88888888-8", name="Sylvia Inactiva", role="lawyer", is_firm_lawyer=True, is_active=False)
    db.add_all([ext, inact])
    db.commit()
    r = client.get("/api/v1/renovaciones/abogados", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    names = {o["nombre"] for o in r.json()}
    assert "Eduardo Venegas" in names
    assert "Contraparte Externa" not in names  # not a firm lawyer
    assert "Sylvia Inactiva" not in names       # inactive


def test_create_derives_hasta_and_total(client, admin, abogado):
    r = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "1000012345",
        "cliente_rut": "17.098.014-k",
        "cliente_nombre": "Camila Cerda",
        "lawyer_id": abogado.id,
        "monto_cuota": 25000,
        "fecha_desde": "2026-07-21",
    })
    assert r.status_code == 201
    b = r.json()
    assert b["fecha_desde"] == "2026-07-21"
    assert b["fecha_hasta"] == "2027-07-21"   # +1 year
    assert b["total"] == 25000 * 12           # cuota × 12
    assert b["cuotas"] == 12
    assert b["cliente_rut"] == "17.098.014-K"  # normalized + formatted
    assert b["lawyer_nombre"] == "Eduardo Venegas"


def test_create_custom_amount(client, admin, abogado):
    r = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "C-9", "cliente_rut": "12345678-5",
        "cliente_nombre": "Otro Cliente", "lawyer_id": abogado.id, "monto_cuota": 40000,
    })
    assert r.status_code == 201
    assert r.json()["total"] == 40000 * 12


def test_create_defaults_amount_and_today(client, admin, abogado):
    r = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "C-1", "cliente_rut": "11111111-1",
        "cliente_nombre": "Cliente X", "lawyer_id": abogado.id,
    })
    assert r.status_code == 201
    b = r.json()
    assert b["monto_cuota"] == 25000            # default
    assert b["fecha_desde"] == date.today().isoformat()


def test_create_rejects_non_firm_lawyer(client, admin, db):
    ext = Lawyer(rut="77777777-7", name="Externo", role="lawyer", is_firm_lawyer=False)
    db.add(ext)
    db.commit()
    db.refresh(ext)
    r = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "C-2", "cliente_rut": "22222222-2",
        "cliente_nombre": "Y", "lawyer_id": ext.id,
    })
    assert r.status_code == 404


def test_create_rejects_bad_amount_and_blank(client, admin, abogado):
    bad_amount = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "C-3", "cliente_rut": "3-3", "cliente_nombre": "Z",
        "lawyer_id": abogado.id, "monto_cuota": 0,
    })
    assert bad_amount.status_code == 400
    blank = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "  ", "cliente_rut": "3-3", "cliente_nombre": "Z",
        "lawyer_id": abogado.id,
    })
    assert blank.status_code == 422


def test_list_filter_and_resumen(client, admin, abogado):
    for m in ("2026-07-05", "2026-07-20", "2026-08-01"):
        client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
            "numero_contrato": f"C-{m}", "cliente_rut": "12345678-5",
            "cliente_nombre": "Cliente", "lawyer_id": abogado.id,
            "monto_cuota": 25000, "fecha_desde": m,
        })
    jul = client.get("/api/v1/renovaciones?periodo=2026-07", headers=_h(ADMIN_RUT)).json()
    assert len(jul) == 2
    res = client.get("/api/v1/renovaciones/resumen?periodo=2026-07", headers=_h(ADMIN_RUT)).json()
    assert res["count"] == 2
    assert res["total_cuotas"] == 50000
    assert res["total_anual"] == 50000 * 12


def test_delete_permissions(client, admin, abogado, db):
    reno = client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
        "numero_contrato": "C-del", "cliente_rut": "12345678-5",
        "cliente_nombre": "Cliente", "lawyer_id": abogado.id,
    }).json()
    # another non-admin who didn't create it → 403
    other = Lawyer(rut="10101010-1", name="Otro", role="lawyer")
    db.add(other)
    db.commit()
    forbidden = client.delete(f"/api/v1/renovaciones/{reno['id']}", headers=_h("10101010-1"))
    assert forbidden.status_code == 403
    # admin → 204
    ok = client.delete(f"/api/v1/renovaciones/{reno['id']}", headers=_h(ADMIN_RUT))
    assert ok.status_code == 204
