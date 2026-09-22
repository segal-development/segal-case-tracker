"""Public hito form by per-lawyer secret link (token).

Lawyers have no app login: the admin issues each one a secret link, and the
public routes under ``/hitos/public/{token}`` let that lawyer submit hitos
(evidence required), see their own, and attach/see evidence — scoped strictly
to the token's lawyer. Covers the admin link lifecycle too.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo, HITO_APROBADO, HITO_PENDIENTE
from app.models.lawyer import Lawyer
from app.services import storage_service

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"
OTHER_RUT = "18248270-6"

LINK_INVALIDO = "Link inválido o vencido"
EVIDENCIA_OBLIGATORIA = "La evidencia es obligatoria"


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
    obj = Lawyer(rut=LAWYER_RUT, name="Benjamín Lawyer", role="lawyer", nivel="pleno",
                 hito_form_token="tok-benjamin-0123456789")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def other_lawyer(db):
    obj = Lawyer(rut=OTHER_RUT, name="Otra Abogada", role="lawyer", nivel="junior",
                 hito_form_token="tok-otra-0123456789")
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


def _public(token, suffix=""):
    return f"/api/v1/hitos/public/{token}{suffix}"


def _form(tipo_id, **over):
    """Every mandatory public-form field, overridable per test."""
    data = {
        "hito_tipo_id": tipo_id, "fecha_hito": "2026-07-15", "rol_causa": "C-1-2026",
        "descripcion": "C-100-2026", "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo",
    }
    data.update(over)
    return data


def _post_public(client, token, tipo_id, evidencia=True, extra=None):
    data = _form(tipo_id, **(extra or {}))
    files = {"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")} if evidencia else None
    return client.post(_public(token), data=data, files=files)


def _hito(db, lawyer, tipo, estado=HITO_PENDIENTE, key=None):
    h = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
             fecha_hito=date(2026, 7, 15), estado=estado,
             evidencia_storage_key=key, evidencia_content_type="image/png" if key else None)
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


# --------------------------------------------------------------------------- #
# Admin: form links
# --------------------------------------------------------------------------- #
def test_form_links_lists_active_firm_lawyers(client, db, admin, lawyer, other_lawyer):
    inactive = Lawyer(rut="11111111-1", name="Inactiva", role="lawyer", is_active=False)
    db.add(inactive)
    db.commit()
    r = client.get("/api/v1/hitos/form-links", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    rows = {x["lawyer_id"]: x for x in r.json()}
    assert set(rows) == {lawyer.id, other_lawyer.id}  # admin (not firm) and inactive excluded
    row = rows[lawyer.id]
    assert row == {
        "lawyer_id": lawyer.id, "nombre": lawyer.name, "rut": LAWYER_RUT, "nivel": "pleno",
        "tiene_link": True, "token": lawyer.hito_form_token,
    }


def test_form_links_generate_is_idempotent_and_regenerar_replaces(client, db, admin):
    lw = Lawyer(rut="22222222-2", name="Nuevo", role="lawyer")
    db.add(lw)
    db.commit()
    db.refresh(lw)

    r = client.post(f"/api/v1/hitos/form-links/{lw.id}", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    body = r.json()
    assert body["lawyer_id"] == lw.id and body["nombre"] == "Nuevo"
    token = body["token"]
    assert len(token) >= 32

    # same call again → same token
    r = client.post(f"/api/v1/hitos/form-links/{lw.id}", headers=_h(ADMIN_RUT))
    assert r.json()["token"] == token

    # regenerar → new token, old one dead
    r = client.post(f"/api/v1/hitos/form-links/{lw.id}?regenerar=true", headers=_h(ADMIN_RUT))
    new_token = r.json()["token"]
    assert new_token != token
    assert client.get(_public(token)).status_code == 404
    assert client.get(_public(new_token)).status_code == 200


def test_form_links_revoke(client, db, admin, lawyer):
    token = lawyer.hito_form_token
    r = client.delete(f"/api/v1/hitos/form-links/{lawyer.id}", headers=_h(ADMIN_RUT))
    assert r.status_code == 204
    db.refresh(lawyer)
    assert lawyer.hito_form_token is None
    row = next(x for x in client.get("/api/v1/hitos/form-links", headers=_h(ADMIN_RUT)).json()
               if x["lawyer_id"] == lawyer.id)
    assert row["tiene_link"] is False and row["token"] is None
    assert client.get(_public(token)).status_code == 404


def test_form_links_404_for_non_firm_or_inactive(client, db, admin):
    r = client.post(f"/api/v1/hitos/form-links/{admin.id}", headers=_h(ADMIN_RUT))
    assert r.status_code == 404  # admin fixture is not a firm lawyer
    assert client.post("/api/v1/hitos/form-links/999999", headers=_h(ADMIN_RUT)).status_code == 404


def test_form_links_require_admin(client, db, admin, lawyer):
    assert client.get("/api/v1/hitos/form-links", headers=_h(LAWYER_RUT)).status_code == 403
    assert client.post(f"/api/v1/hitos/form-links/{lawyer.id}", headers=_h(LAWYER_RUT)).status_code == 403
    assert client.delete(f"/api/v1/hitos/form-links/{lawyer.id}", headers=_h(LAWYER_RUT)).status_code == 403


# --------------------------------------------------------------------------- #
# Public: GET form
# --------------------------------------------------------------------------- #
def test_public_form_returns_abogado_and_tipos(client, db, lawyer, tipo):
    r = client.get(_public(lawyer.hito_form_token))  # no Authorization header
    assert r.status_code == 200
    body = r.json()
    assert body["abogado"] == {"id": lawyer.id, "nombre": lawyer.name, "rut": LAWYER_RUT, "nivel": "pleno"}
    assert [t["id"] for t in body["tipos"]] == [tipo.id]
    assert body["tipos"][0]["valor_bruto"] == 8077


def test_public_form_invalid_token_404(client, db, lawyer):
    r = client.get(_public("does-not-exist"))
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO


def test_public_form_inactive_lawyer_404(client, db, lawyer):
    lawyer.is_active = False
    db.commit()
    r = client.get(_public(lawyer.hito_form_token))
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO


def test_public_route_not_shadowed_by_hito_id_routes(client, db, lawyer):
    """``/hitos/public/abc`` must resolve to the public route (404 link), never to a
    ``/{hito_id}`` route (which would answer 401/422)."""
    for r in (client.get(_public("abc")), client.get(_public("abc", "/hitos")),
              client.get(_public("abc", "/hitos/1/evidencia"))):
        assert r.status_code == 404
        assert r.json()["detail"] == LINK_INVALIDO
    r = client.post(_public("abc"), data={"hito_tipo_id": 1, "fecha_hito": "2026-07-15"},
                    files={"evidencia": ("c.png", b"x", "image/png")})
    assert r.status_code == 404 and r.json()["detail"] == LINK_INVALIDO


# --------------------------------------------------------------------------- #
# Public: POST hito
# --------------------------------------------------------------------------- #
def test_public_post_without_evidencia_422(client, db, storage, lawyer, tipo):
    r = _post_public(client, lawyer.hito_form_token, tipo.id, evidencia=False)
    assert r.status_code == 422
    assert r.json()["detail"] == EVIDENCIA_OBLIGATORIA
    r = client.post(_public(lawyer.hito_form_token), data=_form(tipo.id),
                    files={"evidencia": ("cap.png", b"", "image/png")})
    assert r.status_code == 422
    assert r.json()["detail"] == EVIDENCIA_OBLIGATORIA
    assert db.query(Hito).count() == 0
    assert storage.store == {}


@pytest.mark.parametrize("field", ["hito_tipo_id", "fecha_hito", "rol_causa", "descripcion", "tribunal", "procedimiento"])
def test_public_post_missing_field_422(client, db, storage, lawyer, tipo, field):
    """Every business field is mandatory server-side: absent → 422 naming the field."""
    data = _form(tipo.id)
    data.pop(field)
    r = client.post(_public(lawyer.hito_form_token), data=data,
                    files={"evidencia": ("cap.png", b"\x89PNG", "image/png")})
    assert r.status_code == 422
    assert r.json()["detail"] == f"El campo {field} es obligatorio"
    assert db.query(Hito).count() == 0
    assert storage.store == {}


@pytest.mark.parametrize("field", ["rol_causa", "descripcion", "tribunal", "procedimiento"])
def test_public_post_blank_field_422(client, db, storage, lawyer, tipo, field):
    """Whitespace-only text does not satisfy a mandatory field."""
    r = client.post(_public(lawyer.hito_form_token), data=_form(tipo.id, **{field: "   "}),
                    files={"evidencia": ("cap.png", b"\x89PNG", "image/png")})
    assert r.status_code == 422
    assert r.json()["detail"] == f"El campo {field} es obligatorio"
    assert db.query(Hito).count() == 0


def test_public_post_sysgal_fields_stay_optional(client, db, storage, lawyer, tipo):
    r = _post_public(client, lawyer.hito_form_token, tipo.id)
    assert r.status_code == 201
    assert r.json()["etapa_sysgal"] == tipo.etapa_tramite  # defaulted from the tipo
    assert r.json()["tramite_sysgal"] is None


def test_public_post_creates_hito_for_token_lawyer(client, db, storage, lawyer, tipo):
    r = _post_public(client, lawyer.hito_form_token, tipo.id,
                     extra={"descripcion": "6147-2026", "tribunal": "1º Juzgado Civil"})
    assert r.status_code == 201
    body = r.json()
    assert body["lawyer_id"] == lawyer.id
    assert body["estado"] == HITO_PENDIENTE
    assert body["origen"] == "formulario"
    assert body["tiene_evidencia"] is True
    assert body["valor_bruto"] == 8077
    assert body["descripcion"] == "C-6147-2026"  # same normalization as the authenticated create
    assert body["created_by_name"] == lawyer.name
    h = db.get(Hito, body["id"])
    assert h.created_by_rut == LAWYER_RUT
    assert h.evidencia_storage_key.startswith(f"hitos/evidencia/{lawyer.id}/")
    assert h.evidencia_storage_key in storage.store


def test_public_post_ignores_lawyer_id_field(client, db, storage, lawyer, other_lawyer, tipo):
    r = _post_public(client, lawyer.hito_form_token, tipo.id, extra={"lawyer_id": other_lawyer.id})
    assert r.status_code == 201
    assert r.json()["lawyer_id"] == lawyer.id
    assert db.query(Hito).filter(Hito.lawyer_id == other_lawyer.id).count() == 0


def test_public_post_applies_dedup_rule(client, db, storage, lawyer, tipo):
    assert _post_public(client, lawyer.hito_form_token, tipo.id, extra={"descripcion": "C-9-2026"}).status_code == 201
    r = _post_public(client, lawyer.hito_form_token, tipo.id, extra={"descripcion": "C-9-2026"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Ya existe un hito de este abogado para esa causa"


def test_public_post_invalid_content_type_415(client, db, storage, lawyer, tipo):
    r = client.post(_public(lawyer.hito_form_token), data=_form(tipo.id),
                    files={"evidencia": ("cap.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 415


# --------------------------------------------------------------------------- #
# Public: list own hitos
# --------------------------------------------------------------------------- #
def test_public_list_only_own_newest_first(client, db, lawyer, other_lawyer, tipo):
    older = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
                 fecha_hito=date(2026, 6, 1), estado=HITO_APROBADO)
    newer = Hito(lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
                 fecha_hito=date(2026, 7, 20), estado="rechazado", rechazo_motivo="Falta captura")
    foreign = Hito(lawyer_id=other_lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
                   fecha_hito=date(2026, 7, 25), estado=HITO_PENDIENTE)
    db.add_all([older, newer, foreign])
    db.commit()

    r = client.get(_public(lawyer.hito_form_token, "/hitos"))
    assert r.status_code == 200
    items = r.json()
    assert [x["id"] for x in items] == [newer.id, older.id]
    assert items[0]["estado"] == "rechazado"
    assert items[0]["rechazo_motivo"] == "Falta captura"
    assert items[0]["tiene_evidencia"] is False
    assert items[0]["tipo_label"] == "Prescripción terminada"
    assert items[0]["valor_bruto"] == 8077


# --------------------------------------------------------------------------- #
# Public: evidencia PUT / GET
# --------------------------------------------------------------------------- #
def test_public_put_evidencia_own_hito_then_get(client, db, storage, lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = client.put(_public(lawyer.hito_form_token, f"/hitos/{h.id}/evidencia"),
                   files={"evidencia": ("cap.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 200
    assert r.json()["tiene_evidencia"] is True
    db.refresh(h)
    assert h.evidencia_content_type == "application/pdf"

    r = client.get(_public(lawyer.hito_form_token, f"/hitos/{h.id}/evidencia"))
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 fake"
    assert r.headers["content-type"].startswith("application/pdf")


def test_public_put_evidencia_on_aprobado_409(client, db, storage, lawyer, tipo):
    h = _hito(db, lawyer, tipo, estado=HITO_APROBADO, key="hitos/evidencia/x/old.png")
    r = client.put(_public(lawyer.hito_form_token, f"/hitos/{h.id}/evidencia"),
                   files={"evidencia": ("cap.png", b"\x89PNG", "image/png")})
    assert r.status_code == 409
    db.refresh(h)
    assert h.evidencia_storage_key == "hitos/evidencia/x/old.png"


def test_public_evidencia_someone_elses_hito_404(client, db, storage, lawyer, other_lawyer, tipo):
    storage.store["hitos/evidencia/x/theirs.png"] = b"secret"
    theirs = _hito(db, other_lawyer, tipo, key="hitos/evidencia/x/theirs.png")
    r = client.put(_public(lawyer.hito_form_token, f"/hitos/{theirs.id}/evidencia"),
                   files={"evidencia": ("cap.png", b"\x89PNG", "image/png")})
    assert r.status_code == 404
    r = client.get(_public(lawyer.hito_form_token, f"/hitos/{theirs.id}/evidencia"))
    assert r.status_code == 404
    db.refresh(theirs)
    assert theirs.evidencia_storage_key == "hitos/evidencia/x/theirs.png"


# --------------------------------------------------------------------------- #
# Revoked token → 404 everywhere
# --------------------------------------------------------------------------- #
def test_revoked_token_404_on_all_public_routes(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo, key="hitos/evidencia/x/cap.png")
    token = lawyer.hito_form_token
    assert client.delete(f"/api/v1/hitos/form-links/{lawyer.id}", headers=_h(ADMIN_RUT)).status_code == 204

    responses = [
        client.get(_public(token)),
        _post_public(client, token, tipo.id),
        client.get(_public(token, "/hitos")),
        client.put(_public(token, f"/hitos/{h.id}/evidencia"),
                   files={"evidencia": ("cap.png", b"\x89PNG", "image/png")}),
        client.get(_public(token, f"/hitos/{h.id}/evidencia")),
    ]
    for r in responses:
        assert r.status_code == 404
        assert r.json()["detail"] == LINK_INVALIDO
    assert db.query(Hito).count() == 1  # the POST created nothing
