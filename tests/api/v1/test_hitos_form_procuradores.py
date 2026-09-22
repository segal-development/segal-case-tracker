"""Shared procuradores hito form (one generic token).

The firm's procuradores (assistants) get ONE shared link, pick the lawyer they
work for and submit a hito on their behalf with the same mandatory fields +
evidence as the per-lawyer public form. Covers the admin link lifecycle, route
ordering against ``/form-links/{lawyer_id}``, and the public GET/POST.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo, HITO_PENDIENTE, ORIGEN_FORMULARIO
from app.models.hito_form_link import FORM_LINK_KIND_PROCURADORES, HitoFormLink
from app.models.lawyer import Lawyer
from app.services import storage_service

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"
OTHER_RUT = "18248270-6"
TOKEN = "tok-procuradores-0123456789"

LINK_INVALIDO = "Link inválido o vencido"
ABOGADO_INVALIDO = "Selecciona un abogado válido"
EVIDENCIA_OBLIGATORIA = "La evidencia es obligatoria"
ADMIN_URL = "/api/v1/hitos/form-links/procuradores"


class _FakeBackend:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def upload(self, data: bytes, key: str, content_type: str = "application/pdf") -> str:
        self.store[key] = data
        return key

    def retrieve(self, key: str) -> bytes:
        return self.store[key]


@pytest.fixture
def storage(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(storage_service, "get_storage_backend", lambda *_a, **_k: backend)
    return backend


@pytest.fixture
def admin(db):
    obj = Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin", is_firm_lawyer=False)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Benjamín Lawyer", role="lawyer", nivel="pleno")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other_lawyer(db):
    obj = Lawyer(rut=OTHER_RUT, name="Ana Abogada", role="lawyer", nivel="junior")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def inactive_lawyer(db):
    obj = Lawyer(rut="11111111-1", name="Inactiva", role="lawyer", is_active=False)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def procurador_account(db):
    """A procurador ACCOUNT row (role=procurador, not a firm lawyer) — must not be selectable."""
    obj = Lawyer(rut="22222222-2", name="Pedro Procurador", role="procurador", is_firm_lawyer=False)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def link(db):
    row = HitoFormLink(kind=FORM_LINK_KIND_PROCURADORES, token=TOKEN)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


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


def _public(token=TOKEN):
    return f"/api/v1/hitos/public-procuradores/{token}"


def _form(tipo_id, lawyer_id, **over):
    data = {
        "lawyer_id": lawyer_id, "hito_tipo_id": tipo_id, "fecha_hito": "2026-07-15",
        "rol_causa": "C-1-2026", "descripcion": "C-100-2026",
        "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo",
    }
    data.update(over)
    return data


def _post(client, data, token=TOKEN, evidencia=True):
    files = {"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")} if evidencia else None
    return client.post(_public(token), data=data, files=files)


# --------------------------------------------------------------------------- #
# Admin: shared link lifecycle
# --------------------------------------------------------------------------- #
def test_admin_get_without_link(client, db, admin):
    r = client.get(ADMIN_URL, headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert r.json() == {"kind": "procuradores", "tiene_link": False, "token": None}


def test_admin_generate_idempotent_and_regenerar(client, db, admin, tipo):
    r = client.post(ADMIN_URL, headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "procuradores"
    token = body["token"]
    assert len(token) >= 32

    assert client.post(ADMIN_URL, headers=_h(ADMIN_RUT)).json()["token"] == token  # idempotent
    assert client.get(ADMIN_URL, headers=_h(ADMIN_RUT)).json() == {
        "kind": "procuradores", "tiene_link": True, "token": token,
    }
    assert client.get(_public(token)).status_code == 200

    new_token = client.post(ADMIN_URL + "?regenerar=true", headers=_h(ADMIN_RUT)).json()["token"]
    assert new_token != token
    assert client.get(_public(token)).status_code == 404
    assert client.get(_public(new_token)).status_code == 200
    assert db.query(HitoFormLink).count() == 1  # one row per kind, always


def test_admin_revoke(client, db, admin, link):
    r = client.delete(ADMIN_URL, headers=_h(ADMIN_RUT))
    assert r.status_code == 204
    db.refresh(link)
    assert link.token is None
    assert client.get(ADMIN_URL, headers=_h(ADMIN_RUT)).json()["tiene_link"] is False
    assert client.get(_public(TOKEN)).status_code == 404
    assert client.delete(ADMIN_URL, headers=_h(ADMIN_RUT)).status_code == 204  # idempotent


def test_admin_requires_admin(client, db, admin, lawyer, link):
    assert client.get(ADMIN_URL, headers=_h(LAWYER_RUT)).status_code == 403
    assert client.post(ADMIN_URL, headers=_h(LAWYER_RUT)).status_code == 403
    assert client.delete(ADMIN_URL, headers=_h(LAWYER_RUT)).status_code == 403
    assert client.get(ADMIN_URL).status_code in (401, 403)  # no token at all


def test_route_order_procuradores_not_parsed_as_lawyer_id(client, db, admin, lawyer):
    """``/form-links/procuradores`` must hit the literal routes, never
    ``/form-links/{lawyer_id}`` (which would answer 422 for a non-int id)."""
    r = client.post(ADMIN_URL, headers=_h(ADMIN_RUT))
    assert r.status_code == 200 and r.json()["kind"] == "procuradores"
    assert client.delete(ADMIN_URL, headers=_h(ADMIN_RUT)).status_code == 204
    # and the per-lawyer route still works with a real id
    r = client.post(f"/api/v1/hitos/form-links/{lawyer.id}", headers=_h(ADMIN_RUT))
    assert r.status_code == 200 and r.json()["lawyer_id"] == lawyer.id
    # the per-lawyer list does not include the shared link
    assert all("kind" not in row for row in client.get("/api/v1/hitos/form-links", headers=_h(ADMIN_RUT)).json())


# --------------------------------------------------------------------------- #
# Public: GET form
# --------------------------------------------------------------------------- #
def test_public_get_lists_tipos_and_active_firm_lawyers(
    client, db, link, tipo, admin, lawyer, other_lawyer, inactive_lawyer, procurador_account,
):
    r = client.get(_public())  # no Authorization header
    assert r.status_code == 200
    body = r.json()
    assert [t["id"] for t in body["tipos"]] == [tipo.id]
    assert body["tipos"][0]["valor_bruto"] == 8077
    # only active firm lawyers, ordered by nombre; admin (non-firm), inactive and procurador excluded
    assert body["abogados"] == [
        {"id": other_lawyer.id, "nombre": "Ana Abogada", "rut": OTHER_RUT, "nivel": "junior"},
        {"id": lawyer.id, "nombre": "Benjamín Lawyer", "rut": LAWYER_RUT, "nivel": "pleno"},
    ]


def test_public_get_invalid_token_404(client, db, link):
    r = client.get(_public("nope"))
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO


def test_public_get_revoked_token_404(client, db, link):
    link.token = None
    db.commit()
    r = client.get(_public(TOKEN))
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO


def test_public_procuradores_token_does_not_open_per_lawyer_form(client, db, link, lawyer):
    """The shared token is not a per-lawyer token and vice versa."""
    assert client.get(f"/api/v1/hitos/public/{TOKEN}").status_code == 404
    lawyer.hito_form_token = "tok-benjamin-0123456789"
    db.commit()
    assert client.get(_public("tok-benjamin-0123456789")).status_code == 404


# --------------------------------------------------------------------------- #
# Public: POST hito
# --------------------------------------------------------------------------- #
def test_public_post_ok_creates_for_selected_lawyer(client, db, storage, link, tipo, lawyer, other_lawyer):
    r = _post(client, _form(tipo.id, other_lawyer.id, descripcion="6147-2026"))
    assert r.status_code == 201
    body = r.json()
    assert body["lawyer_id"] == other_lawyer.id
    assert body["lawyer_nombre"] == "Ana Abogada"
    assert body["estado"] == HITO_PENDIENTE
    assert body["origen"] == ORIGEN_FORMULARIO
    assert body["created_by_name"] == "Procurador (link genérico)"
    assert body["tiene_evidencia"] is True
    assert body["valor_bruto"] == 8077
    assert body["descripcion"] == "C-6147-2026"  # same normalization as every create
    h = db.get(Hito, body["id"])
    assert h.created_by_rut == "procurador"
    assert h.evidencia_storage_key.startswith(f"hitos/evidencia/{other_lawyer.id}/")
    assert h.evidencia_storage_key in storage.store
    assert db.query(Hito).filter(Hito.lawyer_id == lawyer.id).count() == 0


@pytest.mark.parametrize("bad", [None, 999999, "inactive", "procurador", "admin"])
def test_public_post_invalid_lawyer_422(client, db, storage, link, tipo, admin, inactive_lawyer, procurador_account, bad):
    lawyer_id = {"inactive": inactive_lawyer.id, "procurador": procurador_account.id, "admin": admin.id}.get(bad, bad)
    data = _form(tipo.id, lawyer_id)
    if lawyer_id is None:
        data.pop("lawyer_id")
    r = _post(client, data)
    assert r.status_code == 422
    assert r.json()["detail"] == ABOGADO_INVALIDO
    assert db.query(Hito).count() == 0
    assert storage.store == {}


@pytest.mark.parametrize("field", ["hito_tipo_id", "fecha_hito", "rol_causa", "descripcion", "tribunal", "procedimiento"])
def test_public_post_missing_field_422(client, db, storage, link, tipo, lawyer, field):
    data = _form(tipo.id, lawyer.id)
    data.pop(field)
    r = _post(client, data)
    assert r.status_code == 422
    assert r.json()["detail"] == f"El campo {field} es obligatorio"
    assert db.query(Hito).count() == 0


@pytest.mark.parametrize("field", ["rol_causa", "descripcion", "tribunal", "procedimiento"])
def test_public_post_blank_field_422(client, db, storage, link, tipo, lawyer, field):
    r = _post(client, _form(tipo.id, lawyer.id, **{field: "   "}))
    assert r.status_code == 422
    assert r.json()["detail"] == f"El campo {field} es obligatorio"


def test_public_post_without_evidencia_422(client, db, storage, link, tipo, lawyer):
    r = _post(client, _form(tipo.id, lawyer.id), evidencia=False)
    assert r.status_code == 422
    assert r.json()["detail"] == EVIDENCIA_OBLIGATORIA
    r = client.post(_public(), data=_form(tipo.id, lawyer.id),
                    files={"evidencia": ("cap.png", b"", "image/png")})
    assert r.status_code == 422
    assert r.json()["detail"] == EVIDENCIA_OBLIGATORIA
    assert db.query(Hito).count() == 0
    assert storage.store == {}


def test_public_post_invalid_token_404(client, db, storage, link, tipo, lawyer):
    r = _post(client, _form(tipo.id, lawyer.id), token="nope")
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO
    assert db.query(Hito).count() == 0


def test_public_post_dedup_applies(client, db, storage, link, tipo, lawyer):
    assert _post(client, _form(tipo.id, lawyer.id)).status_code == 201
    r = _post(client, _form(tipo.id, lawyer.id))
    assert r.status_code == 409
    assert r.json()["detail"] == "Ya existe un hito de este abogado para esa causa"


def test_public_post_formulario_hito_requires_evidence_to_approve(client, db, storage, link, tipo, lawyer, admin):
    """A procurador-submitted hito is origen=formulario, so the approve gate applies (defensive)."""
    hid = _post(client, _form(tipo.id, lawyer.id)).json()["id"]
    assert client.post(f"/api/v1/hitos/{hid}/aprobar", headers=_h(ADMIN_RUT)).status_code == 200
