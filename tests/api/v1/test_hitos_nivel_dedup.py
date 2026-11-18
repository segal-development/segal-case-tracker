"""Nivel-aware hito duplicate rule: one Junior and one Pleno hito per lawyer + causa.

A lawyer promoted from Junior to Pleno must be able to file a Pleno hito on a causa
where he already has a Junior one (prod case: C-1588-2022). Two hitos of the SAME
nivel on the same causa (+ tolerant tribunal) still collide, with the same 409 text.
"""
import io
from datetime import date, datetime

import openpyxl
import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo
from app.models.hito_form_link import FORM_LINK_KIND_PROCURADORES, HitoFormLink
from app.models.lawyer import Lawyer
from app.services import storage_service

ADMIN_RUT = "16021492-9"
CLIENT_RUT = "15.326.356-6"
ROL = "C-1588-2022"
TRIB = "2º Juzgado Civil de Concepción"
TOKEN = "tok-gonzalo-0123456789"
PROC_TOKEN = "tok-procuradores-0123456789"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DUP_PREFIX = "Ya existe un hito de este abogado para esa causa"


class _FakeBackend:
    def upload(self, data, key, content_type="application/pdf"):
        return key

    def retrieve(self, key):
        return b""


@pytest.fixture(autouse=True)
def _storage(monkeypatch):
    monkeypatch.setattr(storage_service, "get_storage_backend", lambda *_a, **_k: _FakeBackend())


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin")
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="19813311-6", name="Gonzalo Caldeorn", role="lawyer", is_firm_lawyer=True,
                 hito_form_token=TOKEN)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other_lawyer(db):
    obj = Lawyer(rut="18248270-6", name="Otra Abogada", role="lawyer", is_firm_lawyer=True)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _tipo(db, code, nivel, orden, label):
    t = HitoTipo(code=code, label=label, nivel=nivel, valor_bruto=1000, orden=orden)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@pytest.fixture
def junior(db):
    return _tipo(db, "junior_h1_conversion", "junior", 1, "Conversión preventiva")


@pytest.fixture
def junior2(db):
    return _tipo(db, "junior_h2_otro", "junior", 2, "Otro junior")


@pytest.fixture
def pleno(db):
    return _tipo(db, "pleno_prescripcion", "pleno", 3, "Prescripción terminada")


@pytest.fixture
def pleno2(db):
    return _tipo(db, "pleno_excepcion_dilatoria", "pleno", 4, "Excepción dilatoria acogida")


def _seed(db, lawyer, tipo, *, estado="aprobado", tribunal=TRIB, rol=ROL, origen=None):
    h = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=1000, fecha_hito=date(2026, 8, 6),
             rol_causa=CLIENT_RUT, descripcion=rol, tribunal=tribunal, estado=estado)
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _create(client, tipo, lawyer, *, tribunal=TRIB):
    return client.post(
        "/api/v1/hitos", headers=_h(ADMIN_RUT),
        data={"hito_tipo_id": tipo.id, "fecha_hito": "2026-08-31", "lawyer_id": lawyer.id,
              "rol_causa": CLIENT_RUT, "descripcion": ROL, "tribunal": tribunal or ""},
    )


def _put(client, hito, tipo):
    return client.put(
        f"/api/v1/hitos/{hito.id}", headers=_h(ADMIN_RUT),
        json={"hito_tipo_id": tipo.id, "fecha_hito": "2026-08-06", "rol_causa": CLIENT_RUT,
              "descripcion": ROL, "tribunal": TRIB},
    )


# --------------------------------------------------------------------------- #
# Manual create
# --------------------------------------------------------------------------- #
def test_create_pleno_allowed_when_junior_exists(client, db, admin, lawyer, junior, pleno):
    _seed(db, lawyer, junior)
    r = _create(client, pleno, lawyer)
    assert r.status_code == 201, r.text
    assert db.query(Hito).count() == 2


def test_create_junior_allowed_when_pleno_exists(client, db, admin, lawyer, junior, pleno):
    _seed(db, lawyer, pleno)
    r = _create(client, junior, lawyer)
    assert r.status_code == 201, r.text
    assert db.query(Hito).count() == 2


def test_create_junior_plus_junior_still_409_same_text(client, db, admin, lawyer, junior, junior2):
    _seed(db, lawyer, junior)
    r = _create(client, junior2, lawyer)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        "Ya existe un hito de este abogado para esa causa con fecha 06-08-2026 y está aprobado."
    )
    assert db.query(Hito).count() == 1


def test_create_pleno_plus_pleno_still_409(client, db, admin, lawyer, pleno, pleno2):
    _seed(db, lawyer, pleno)
    r = _create(client, pleno2, lawyer)
    assert r.status_code == 409, r.text
    assert r.json()["detail"].startswith(DUP_PREFIX)
    assert db.query(Hito).count() == 1


def test_create_same_nivel_tolerant_blank_tribunal_still_409(client, db, admin, lawyer, junior, junior2):
    _seed(db, lawyer, junior, tribunal=None)
    assert _create(client, junior2, lawyer).status_code == 409


def test_create_pleno_after_rejected_junior_and_rejected_pleno_allowed(client, db, admin, lawyer, pleno, pleno2):
    _seed(db, lawyer, pleno, estado="rechazado")
    assert _create(client, pleno2, lawyer).status_code == 201


def test_create_other_lawyer_never_collides(client, db, admin, lawyer, other_lawyer, junior, junior2):
    _seed(db, lawyer, junior)
    assert _create(client, junior2, other_lawyer).status_code == 201


# --------------------------------------------------------------------------- #
# PUT (update): the NEW tipo's nivel is what counts
# --------------------------------------------------------------------------- #
def test_put_to_other_nivel_no_longer_collides_with_sibling(client, db, admin, lawyer, junior, pleno):
    _seed(db, lawyer, junior)
    mine = _seed(db, lawyer, junior, tribunal="8º Juzgado Civil de Santiago")
    # distinct causa so both rows exist, then edit `mine` onto the sibling's causa as pleno
    r = _put(client, mine, pleno)
    assert r.status_code == 200, r.text


def test_put_to_same_nivel_still_collides_with_sibling(client, db, admin, lawyer, junior, junior2, pleno):
    _seed(db, lawyer, junior)
    mine = _seed(db, lawyer, pleno, tribunal="8º Juzgado Civil de Santiago")
    r = _put(client, mine, junior2)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Ya existe un hito de este abogado para esa causa"


# --------------------------------------------------------------------------- #
# Approval: "otro aprobado" is scoped by nivel
# --------------------------------------------------------------------------- #
def test_aprobar_pleno_allowed_while_junior_aprobado(client, db, admin, lawyer, junior, pleno):
    _seed(db, lawyer, junior, estado="aprobado")
    p = _seed(db, lawyer, pleno, estado="pendiente")
    r = client.post(f"/api/v1/hitos/{p.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "aprobado"


def test_aprobar_second_same_nivel_still_blocked(client, db, admin, lawyer, pleno, pleno2):
    _seed(db, lawyer, pleno, estado="aprobado")
    p = _seed(db, lawyer, pleno2, estado="pendiente")
    r = client.post(f"/api/v1/hitos/{p.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Esta causa ya tiene un hito aprobado de este abogado; no se puede aprobar otro."


def test_aprobar_lote_pleno_with_junior_aprobado_is_approved(client, db, admin, lawyer, junior, pleno):
    _seed(db, lawyer, junior, estado="aprobado")
    p = _seed(db, lawyer, pleno, estado="pendiente")
    r = client.post("/api/v1/hitos/aprobar-lote", headers=_h(ADMIN_RUT), json={"ids": [p.id]})
    assert r.status_code == 200, r.text
    db.refresh(p)
    assert p.estado == "aprobado"


# --------------------------------------------------------------------------- #
# Public forms + /verificar previews
# --------------------------------------------------------------------------- #
def _form(tipo, **over):
    data = {"hito_tipo_id": tipo.id, "fecha_hito": "2026-08-31", "rol_causa": CLIENT_RUT,
            "descripcion": ROL, "tribunal": TRIB, "procedimiento": "Ejecutivo"}
    data.update(over)
    return data


_EV = {"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")}


def test_public_form_pleno_allowed_when_junior_exists(client, db, lawyer, junior, pleno):
    _seed(db, lawyer, junior)
    r = client.post(f"/api/v1/hitos/public/{TOKEN}", data=_form(pleno), files=_EV)
    assert r.status_code == 201, r.text


def test_public_form_junior_plus_junior_still_409(client, db, lawyer, junior, junior2):
    _seed(db, lawyer, junior)
    r = client.post(f"/api/v1/hitos/public/{TOKEN}", data=_form(junior2), files=_EV)
    assert r.status_code == 409, r.text
    assert r.json()["detail"].startswith(DUP_PREFIX)


@pytest.fixture
def proc_link(db):
    row = HitoFormLink(kind=FORM_LINK_KIND_PROCURADORES, token=PROC_TOKEN)
    db.add(row)
    db.commit()
    return row


def test_procuradores_form_pleno_allowed_when_junior_exists(client, db, proc_link, lawyer, junior, pleno):
    _seed(db, lawyer, junior)
    r = client.post(f"/api/v1/hitos/public-procuradores/{PROC_TOKEN}",
                    data=_form(pleno, lawyer_id=lawyer.id), files=_EV)
    assert r.status_code == 201, r.text


def test_procuradores_form_junior_plus_junior_still_409(client, db, proc_link, lawyer, junior, junior2):
    _seed(db, lawyer, junior)
    r = client.post(f"/api/v1/hitos/public-procuradores/{PROC_TOKEN}",
                    data=_form(junior2, lawyer_id=lawyer.id), files=_EV)
    assert r.status_code == 409, r.text


_CAUSA = {"rol_causa": CLIENT_RUT, "descripcion": ROL, "tribunal": TRIB}


def test_verificar_other_nivel_reports_free(client, db, lawyer, junior, pleno):
    _seed(db, lawyer, junior)
    r = client.get(f"/api/v1/hitos/public/{TOKEN}/verificar", params={**_CAUSA, "hito_tipo_id": pleno.id})
    assert r.json() == {"existe": False, "detalle": None}


def test_verificar_same_nivel_reports_collision(client, db, lawyer, junior, junior2):
    _seed(db, lawyer, junior)
    r = client.get(f"/api/v1/hitos/public/{TOKEN}/verificar", params={**_CAUSA, "hito_tipo_id": junior2.id})
    assert r.json()["existe"] is True
    assert r.json()["detalle"].startswith(DUP_PREFIX)


def test_verificar_without_tipo_keeps_any_type_answer(client, db, lawyer, junior):
    _seed(db, lawyer, junior)
    r = client.get(f"/api/v1/hitos/public/{TOKEN}/verificar", params=_CAUSA)
    assert r.json()["existe"] is True


def test_verificar_procuradores_other_nivel_free_same_nivel_collides(
    client, db, proc_link, lawyer, junior, junior2, pleno
):
    _seed(db, lawyer, junior)
    base = {**_CAUSA, "lawyer_id": lawyer.id}
    url = f"/api/v1/hitos/public-procuradores/{PROC_TOKEN}/verificar"
    assert client.get(url, params={**base, "hito_tipo_id": pleno.id}).json()["existe"] is False
    assert client.get(url, params={**base, "hito_tipo_id": junior2.id}).json()["existe"] is True
    assert client.get(url, params=base).json()["existe"] is True


# --------------------------------------------------------------------------- #
# Excel importer
# --------------------------------------------------------------------------- #
def _import(client, rows):
    """rows: (tipo text, tribunal). Same lawyer, causa."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HITOS"
    ws.append(["ABOGADO", "FECHA", "RUT", "ROL", "TIPO DE HITO", "TRIBUNAL", "APROBADO"])
    for tipo_txt, tribunal in rows:
        ws.append(["Gonzalo Caldeorn", datetime(2026, 8, 12), CLIENT_RUT, ROL, tipo_txt, tribunal, "SI"])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/v1/hitos/importar", headers=_h(ADMIN_RUT),
                    files={"archivo": ("h.xlsx", buf.getvalue(), _XLSX)})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_pleno_allowed_when_junior_exists_in_db(client, db, admin, lawyer, junior, pleno):
    _seed(db, lawyer, junior)
    r = _import(client, [("Prescripción terminada", TRIB)])
    assert r["creadas"] == 1 and r["omitidas_duplicadas"] == 0


def test_import_junior_and_pleno_in_same_file_both_created(client, db, admin, lawyer, junior, pleno):
    r = _import(client, [("Conversión preventiva", TRIB), ("Prescripción terminada", TRIB)])
    assert r["creadas"] == 2 and r["omitidas_duplicadas"] == 0


def test_import_same_nivel_still_duplicate(client, db, admin, lawyer, junior, junior2):
    _seed(db, lawyer, junior)
    r = _import(client, [("Otro junior", TRIB)])
    assert r["creadas"] == 0 and r["omitidas_duplicadas"] == 1


def test_import_same_nivel_duplicate_within_file(client, db, admin, lawyer, pleno, pleno2):
    r = _import(client, [("Prescripción terminada", TRIB), ("Excepción dilatoria acogida", TRIB)])
    assert r["creadas"] == 1 and r["omitidas_duplicadas"] == 1
