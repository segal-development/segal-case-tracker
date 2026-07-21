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


def _make_xlsx(rows):
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AÑO2026"
    ws.append(["RUT", "NOMBRE", "N° CONTRATO", "CUOTAS", "DESDE", "HASTA", "RENOVADOR", "VALOR"])
    for r in rows:
        ws.append(r)
    tot = wb.create_sheet("TOTALES 2025")  # must be skipped
    tot.append(["ignore", "me"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_importar_excel_maps_and_dedups(client, admin, abogado):
    from datetime import datetime as dt
    rows = [
        ["17098014-k", "Cliente Uno", "C-100", 12, dt(2026, 1, 5), dt(2027, 1, 5), "EVENEGAS", 20000],
        ["12345678-5", "Cliente Dos", "C-101", 12, dt(2026, 2, 10), dt(2027, 2, 10), "MVERA", 25000],
        ["", "", "", "", "", "", "", ""],  # blank → skipped
    ]
    data = _make_xlsx(rows)
    r = client.post(
        "/api/v1/renovaciones/importar", headers=_h(ADMIN_RUT),
        files={"archivo": ("reno.xlsx", data, _XLSX_MIME)},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["total_leidas"] == 2
    assert b["creadas"] == 2
    assert b["vinculadas"] == 1   # EVENEGAS → Eduardo Venegas
    assert b["como_texto"] == 1   # MVERA has no system lawyer

    # re-upload of the same file dedups everything
    r2 = client.post(
        "/api/v1/renovaciones/importar", headers=_h(ADMIN_RUT),
        files={"archivo": ("reno.xlsx", data, _XLSX_MIME)},
    )
    assert r2.json()["creadas"] == 0
    assert r2.json()["omitidas_duplicadas"] == 2

    # linked row shows the system lawyer; text-only row shows the raw renovador
    enero = client.get("/api/v1/renovaciones?periodo=2026-01", headers=_h(ADMIN_RUT)).json()
    assert enero[0]["renovador"] == "Eduardo Venegas"
    febrero = client.get("/api/v1/renovaciones?periodo=2026-02", headers=_h(ADMIN_RUT)).json()
    assert febrero[0]["renovador"] == "MVERA"
    assert febrero[0]["lawyer_id"] is None


def test_importar_requires_admin(client, abogado):
    data = _make_xlsx([])
    r = client.post(
        "/api/v1/renovaciones/importar", headers=_h(LAWYER_RUT),
        files={"archivo": ("reno.xlsx", data, _XLSX_MIME)},
    )
    assert r.status_code == 403


def test_recaudacion_by_year(client, admin, abogado):
    for fecha in ("2026-01-05", "2026-01-20", "2026-02-01"):
        client.post("/api/v1/renovaciones", headers=_h(ADMIN_RUT), json={
            "numero_contrato": f"R-{fecha}", "cliente_rut": "12345678-5",
            "cliente_nombre": "Cliente", "lawyer_id": abogado.id,
            "monto_cuota": 25000, "fecha_desde": fecha,
        })
    rec = client.get("/api/v1/renovaciones/recaudacion?anio=2026", headers=_h(ADMIN_RUT)).json()
    assert rec["anio"] == 2026
    assert len(rec["meses"]) == 12
    enero = rec["meses"][0]
    assert enero["mes"] == 1 and enero["count"] == 2
    assert enero["recaudacion"] == 50000
    assert enero["proyeccion_anual"] == 600000  # 50000 × 12
    assert rec["total_recaudacion"] == 75000
    assert rec["total_count"] == 3


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
