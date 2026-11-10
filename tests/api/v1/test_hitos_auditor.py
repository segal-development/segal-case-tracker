"""Read-only Hitos for the ``auditor`` role (no evidence, no writes).

An auditor sees every firm hito (same scope and filters as an admin) plus the
resumen and the monthly stats, but cannot create, change, approve, delete, import,
manage form links or touch evidence. Admin and plain-lawyer behaviour is unchanged.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo
from app.models.lawyer import Lawyer

ADMIN_RUT = "16021492-9"
AUDITOR_RUT = "11111111-1"
LAWYER_RUT = "19643548-4"
OTHER_RUT = "18248270-6"


def _lawyer(db, rut, name, role="lawyer", **kw):
    obj = Lawyer(rut=rut, name=name, role=role, **kw)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def admin(db):
    return _lawyer(db, ADMIN_RUT, "Carla Admin", role="admin")


@pytest.fixture
def auditor(db):
    return _lawyer(db, AUDITOR_RUT, "Juan Auditor", role="auditor")


@pytest.fixture
def lawyer(db):
    return _lawyer(db, LAWYER_RUT, "Benjamín Lawyer", is_firm_lawyer=True)


@pytest.fixture
def other(db):
    return _lawyer(db, OTHER_RUT, "Otra Abogada", is_firm_lawyer=True)


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


@pytest.fixture
def hitos(db, tipo, lawyer, other):
    rows = [
        Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
             fecha_hito=date(2026, 7, 10), rol_causa="C-1-2026", estado="pendiente",
             origen="manual", evidencia_storage_key="hitos/1/cap.png", evidencia_filename="cap.png"),
        Hito(lawyer_id=other.id, hito_tipo_id=tipo.id, valor_bruto=8077,
             fecha_hito=date(2026, 7, 11), rol_causa="C-2-2026", estado="aprobado",
             origen="manual"),
        Hito(lawyer_id=other.id, hito_tipo_id=tipo.id, valor_bruto=8077,
             fecha_hito=date(2026, 8, 5), rol_causa="C-3-2026", estado="pendiente",
             origen="manual"),
    ]
    db.add_all(rows)
    db.commit()
    for r in rows:
        db.refresh(r)
    return rows


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


# ---- S1: read access ------------------------------------------------------ #
def test_auditor_lists_all_firm_hitos(client, auditor, hitos):
    r = client.get("/api/v1/hitos", headers=_h(AUDITOR_RUT))
    assert r.status_code == 200
    assert r.json()["total"] == 3
    assert {i["lawyer_nombre"] for i in r.json()["items"]} == {"Benjamín Lawyer", "Otra Abogada"}


def test_auditor_list_filters(client, auditor, hitos, other):
    h = _h(AUDITOR_RUT)
    by_lawyer = client.get(f"/api/v1/hitos?lawyer_id={other.id}", headers=h).json()
    assert by_lawyer["total"] == 2
    assert {i["lawyer_id"] for i in by_lawyer["items"]} == {other.id}
    assert client.get("/api/v1/hitos?periodo=2026-07", headers=h).json()["total"] == 2
    assert client.get("/api/v1/hitos?estado=aprobado", headers=h).json()["total"] == 1
    assert client.get("/api/v1/hitos?periodo=2026-07&estado=pendiente", headers=h).json()["total"] == 1


def test_auditor_resumen_and_stats(client, auditor, hitos):
    h = _h(AUDITOR_RUT)
    r = client.get("/api/v1/hitos/resumen?periodo=2026-07", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 2
    s = client.get("/api/v1/hitos/stats/mensual?desde=2026-07&hasta=2026-08", headers=h)
    assert s.status_code == 200


def test_auditor_tipos_and_abogados(client, auditor, tipo, lawyer):
    h = _h(AUDITOR_RUT)
    assert client.get("/api/v1/hitos/tipos", headers=h).status_code == 200
    assert client.get("/api/v1/hitos/abogados", headers=h).status_code == 200


# ---- S3: nothing new about evidence ---------------------------------------- #
def test_auditor_list_exposes_no_evidence_location(client, auditor, hitos):
    items = client.get("/api/v1/hitos", headers=_h(AUDITOR_RUT)).json()["items"]
    for item in items:
        assert "evidencia_storage_key" not in item
        assert "evidencia_filename" not in item
        assert "hitos/1/cap.png" not in str(item)
    assert any(i["tiene_evidencia"] for i in items)


# ---- S2: writes stay closed ------------------------------------------------ #
def test_auditor_cannot_create(client, db, auditor, tipo):
    r = client.post(
        "/api/v1/hitos", headers=_h(AUDITOR_RUT),
        data={"hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15"},
    )
    assert r.status_code == 403
    assert db.query(Hito).count() == 0


def test_auditor_write_endpoints_403(client, db, auditor, hitos):
    h = _h(AUDITOR_RUT)
    hid = hitos[0].id
    ids = {"ids": [hid]}
    calls = [
        ("post", f"/api/v1/hitos/{hid}/aprobar", {}),
        ("post", f"/api/v1/hitos/{hid}/rechazar", {"json": {"motivo": "x"}}),
        ("post", "/api/v1/hitos/aprobar-lote", {"json": ids}),
        ("post", "/api/v1/hitos/rechazar-lote", {"json": ids}),
        ("post", "/api/v1/hitos/eliminar-lote", {"json": ids}),
        ("put", f"/api/v1/hitos/{hid}", {"json": {"hito_tipo_id": hitos[0].hito_tipo_id, "fecha_hito": "2026-07-10"}}),
        ("delete", f"/api/v1/hitos/{hid}", {}),
        ("post", "/api/v1/hitos/importar", {"files": {"file": ("x.xlsx", b"x", "application/octet-stream")}}),
        ("post", "/api/v1/hitos/importar/hojas", {"files": {"file": ("x.xlsx", b"x", "application/octet-stream")}}),
        ("post", "/api/v1/hitos/importar/preview", {"files": {"file": ("x.xlsx", b"x", "application/octet-stream")}}),
        ("get", "/api/v1/hitos/form-links", {}),
        ("get", "/api/v1/hitos/form-links/procuradores", {}),
        ("post", "/api/v1/hitos/form-links/procuradores", {}),
        ("delete", "/api/v1/hitos/form-links/procuradores", {}),
        ("post", f"/api/v1/hitos/form-links/{hitos[0].lawyer_id}", {}),
        ("delete", f"/api/v1/hitos/form-links/{hitos[0].lawyer_id}", {}),
        ("get", f"/api/v1/hitos/{hid}/evidencia", {}),
        ("put", f"/api/v1/hitos/{hid}/evidencia", {"files": {"evidencia": ("c.png", b"\x89PNG", "image/png")}}),
    ]
    for method, url, kw in calls:
        r = getattr(client, method)(url, headers=h, **kw)
        assert r.status_code == 403, (method, url, r.status_code, r.text)
    assert db.query(Hito).count() == 3
    db.refresh(hitos[0])
    assert hitos[0].estado == "pendiente"


# ---- admin / lawyer unchanged ---------------------------------------------- #
def test_admin_still_sees_all_and_resumen(client, admin, hitos):
    h = _h(ADMIN_RUT)
    assert client.get("/api/v1/hitos", headers=h).json()["total"] == 3
    assert client.get("/api/v1/hitos/resumen?periodo=2026-07", headers=h).status_code == 200
    assert client.get("/api/v1/hitos/stats/mensual", headers=h).status_code == 200


def test_lawyer_still_sees_only_own_and_no_admin_reads(client, lawyer, hitos):
    h = _h(LAWYER_RUT)
    assert client.get("/api/v1/hitos", headers=h).json()["total"] == 1
    assert client.get("/api/v1/hitos/resumen", headers=h).status_code == 403
    assert client.get("/api/v1/hitos/stats/mensual", headers=h).status_code == 403


def test_lawyer_can_still_create(client, db, lawyer, tipo):
    r = client.post(
        "/api/v1/hitos", headers=_h(LAWYER_RUT),
        data={
            "hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15", "rol_causa": "12.345.678-5",
            "descripcion": "C-1-2026", "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo",
        },
        files={"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")},
    )
    assert r.status_code == 201, r.text
    assert db.query(Hito).count() == 1
