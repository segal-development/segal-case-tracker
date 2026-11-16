"""Hitos entered by a procurador on behalf of the lawyers he supports (S5)."""
import pytest

from app.models.hito import HITO_APROBADO, Hito, HitoTipo, ORIGEN_FORMULARIO
from app.services import storage_service
from tests.api.v1.procurador_support import MSG_NOT_SUPPORTED, headers, link, make_lawyer


class _Backend:
    def __init__(self):
        self.up = {}

    def upload(self, data, key, content_type="application/pdf"):
        self.up[key] = data
        return key

    def retrieve(self, key):
        return self.up[key]


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    b = _Backend()
    monkeypatch.setattr(storage_service, "get_storage_backend", lambda *_a, **_k: b)
    return b


@pytest.fixture
def w(db):
    admin = make_lawyer(db, "16021492-9", "Admin", role="admin")
    a = make_lawyer(db, "11111111-1", "Abogado A")
    b = make_lawyer(db, "22222222-2", "Abogado B")
    c = make_lawyer(db, "33333333-3", "Abogado C")
    p1 = make_lawyer(db, "44444444-4", "Proc Uno", role="procurador")
    p2 = make_lawyer(db, "55555555-5", "Proc Dos", role="procurador")
    link(db, p1, a)            # one lawyer
    link(db, p2, a, b)         # two lawyers
    t = HitoTipo(code="t1", label="Tipo 1", nivel="pleno", valor_bruto=1000, orden=1)
    db.add(t)
    db.commit()
    return {"admin": admin, "a": a, "b": b, "c": c, "p1": p1, "p2": p2, "tipo": t}


def _data(tipo, rol="C-1-2026", **over):
    d = {"hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15", "rol_causa": "12.345.678-5",
         "descripcion": rol, "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo"}
    d.update(over)
    return d


def _post(client, rut, data, evidencia=True):
    files = {"evidencia": ("c.png", b"\x89PNG", "image/png")} if evidencia else None
    return client.post("/api/v1/hitos", headers=headers(rut), data=data, files=files)


def test_owner_is_the_lawyer_and_entered_by_is_the_procurador(client, db, w):
    r = _post(client, w["p2"].rut, _data(w["tipo"], lawyer_id=w["b"].id))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["lawyer_id"] == w["b"].id
    assert body["created_by_name"] == "Proc Dos"
    assert body["created_by_rut"] == w["p2"].rut
    h = db.get(Hito, body["id"])
    assert h.lawyer_id == w["b"].id and h.origen == ORIGEN_FORMULARIO and h.estado == "pendiente"


def test_single_lawyer_is_preselected(client, db, w):
    r = _post(client, w["p1"].rut, _data(w["tipo"]))
    assert r.status_code == 201, r.text
    assert r.json()["lawyer_id"] == w["a"].id


def test_two_lawyers_without_choice_is_422(client, db, w):
    r = _post(client, w["p2"].rut, _data(w["tipo"]))
    assert r.status_code == 422
    assert db.query(Hito).count() == 0


def test_unsupported_lawyer_403_exact_message_and_nothing_stored(client, db, w):
    r = _post(client, w["p2"].rut, _data(w["tipo"], lawyer_id=w["c"].id))
    assert r.status_code == 403
    assert r.json()["detail"] == MSG_NOT_SUPPORTED
    assert db.query(Hito).count() == 0


def test_same_validations_as_a_lawyer_evidence_required(client, db, w):
    r = _post(client, w["p1"].rut, _data(w["tipo"]), evidencia=False)
    assert r.status_code == 422
    assert "evidencia" in r.json()["detail"].lower()
    assert db.query(Hito).count() == 0


def test_duplicate_rule_applies_per_lawyer(client, db, w):
    assert _post(client, w["p1"].rut, _data(w["tipo"])).status_code == 201
    r = _post(client, w["p1"].rut, _data(w["tipo"]))
    assert r.status_code == 409


def test_list_shows_only_his_lawyers_hitos(client, db, w):
    _post(client, w["p2"].rut, _data(w["tipo"], rol="C-1-2026", lawyer_id=w["a"].id))
    _post(client, w["p2"].rut, _data(w["tipo"], rol="C-2-2026", lawyer_id=w["b"].id))
    _post(client, w["admin"].rut, {**_data(w["tipo"], rol="C-3-2026"), "lawyer_id": w["c"].id})
    r = client.get("/api/v1/hitos", headers=headers(w["p1"].rut))
    assert {i["lawyer_id"] for i in r.json()["items"]} == {w["a"].id}
    r = client.get("/api/v1/hitos", headers=headers(w["p2"].rut))
    assert {i["lawyer_id"] for i in r.json()["items"]} == {w["a"].id, w["b"].id}
    r = client.get("/api/v1/hitos", headers=headers(w["p2"].rut), params={"lawyer_id": w["c"].id})
    assert r.json()["items"] == []


def test_abogados_selector_lists_only_his_lawyers(client, db, w):
    r = client.get("/api/v1/hitos/abogados", headers=headers(w["p2"].rut))
    assert {x["lawyer_id"] for x in r.json()} == {w["a"].id, w["b"].id}
    r = client.get("/api/v1/hitos/abogados", headers=headers(w["p1"].rut))
    assert [x["lawyer_id"] for x in r.json()] == [w["a"].id]


def test_evidence_of_his_lawyers_hito_is_visible_not_others(client, db, w):
    mine = _post(client, w["p1"].rut, _data(w["tipo"])).json()["id"]
    assert client.get(f"/api/v1/hitos/{mine}/evidencia", headers=headers(w["p1"].rut)).status_code == 200
    other = _post(client, w["admin"].rut, {**_data(w["tipo"], rol="C-9-2026"), "lawyer_id": w["c"].id})
    assert client.get(f"/api/v1/hitos/{other.json()['id']}/evidencia", headers=headers(w["p1"].rut)).status_code == 403


def test_never_approves_or_rejects(client, db, w):
    hid = _post(client, w["p1"].rut, _data(w["tipo"])).json()["id"]
    assert client.post(f"/api/v1/hitos/{hid}/aprobar", headers=headers(w["p1"].rut)).status_code == 403
    assert client.post(f"/api/v1/hitos/{hid}/rechazar", headers=headers(w["p1"].rut), json={"motivo": "x"}).status_code == 403
    assert db.get(Hito, hid).estado == "pendiente"


def test_edit_and_delete_own_pending_hito(client, db, w):
    hid = _post(client, w["p1"].rut, _data(w["tipo"])).json()["id"]
    body = {"hito_tipo_id": w["tipo"].id, "fecha_hito": "2026-07-16", "rol_causa": "12.345.678-5",
            "descripcion": "C-1-2026", "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo"}
    r = client.put(f"/api/v1/hitos/{hid}", headers=headers(w["p1"].rut), json=body)
    assert r.status_code == 200 and r.json()["fecha_hito"] == "2026-07-16"
    assert client.delete(f"/api/v1/hitos/{hid}", headers=headers(w["p1"].rut)).status_code == 204
    assert db.get(Hito, hid) is None


def test_cannot_edit_or_delete_others_or_non_pending(client, db, w):
    by_admin = _post(client, w["admin"].rut, {**_data(w["tipo"], rol="C-5-2026"), "lawyer_id": w["a"].id}).json()["id"]
    assert client.delete(f"/api/v1/hitos/{by_admin}", headers=headers(w["p1"].rut)).status_code == 403
    own = _post(client, w["p1"].rut, _data(w["tipo"], rol="C-6-2026")).json()["id"]
    h = db.get(Hito, own)
    h.estado = HITO_APROBADO
    db.commit()
    assert client.delete(f"/api/v1/hitos/{own}", headers=headers(w["p1"].rut)).status_code == 403
    assert db.get(Hito, own) is not None
    assert db.get(Hito, by_admin) is not None


def test_plain_lawyer_still_cannot_delete_or_edit(client, db, w):
    hid = _post(client, w["p1"].rut, _data(w["tipo"])).json()["id"]
    r = client.delete(f"/api/v1/hitos/{hid}", headers=headers(w["a"].rut))
    assert r.status_code == 403 and r.json()["detail"] == "Requiere rol admin"
