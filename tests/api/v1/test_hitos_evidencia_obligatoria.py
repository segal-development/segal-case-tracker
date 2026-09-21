"""Evidence is mandatory to approve a hito ("sin evidencia no se paga").

Covers the approval gate (single + bulk) and the ``PUT /hitos/{id}/evidencia``
endpoint that attaches or replaces evidence after creation, so a hito created
without a capture can still be completed and approved.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo, HITO_APROBADO, HITO_PENDIENTE, HITO_RECHAZADO
from app.models.lawyer import Lawyer
from app.services import storage_service

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"
OTHER_RUT = "18248270-6"

SIN_EVIDENCIA = "El hito no tiene evidencia adjunta; no se puede aprobar sin evidencia."
ESTADO_NO_PERMITE = "Solo se puede adjuntar evidencia a un hito pendiente o rechazado."


class _FakeBackend:
    """In-memory storage: records uploads, never touches disk."""

    def __init__(self):
        self.uploads: list[tuple[str, bytes, str]] = []

    def upload(self, data: bytes, key: str, content_type: str = "application/pdf") -> str:
        self.uploads.append((key, data, content_type))
        return key

    def retrieve(self, key: str) -> bytes:
        return next(d for k, d, _ in self.uploads if k == key)


@pytest.fixture
def storage(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(storage_service, "get_storage_backend", lambda *_a, **_k: backend)
    return backend


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
def other_lawyer(db):
    obj = Lawyer(rut=OTHER_RUT, name="Otra Abogada", role="lawyer")
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


def _hito(db, lawyer, tipo, estado=HITO_PENDIENTE, evidencia=False):
    h = Hito(
        lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
        fecha_hito=date(2026, 7, 15), estado=estado,
        evidencia_storage_key="hitos/evidencia/x/cap.png" if evidencia else None,
        evidencia_content_type="image/png" if evidencia else None,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _put(client, headers, hito_id, filename="cap.png", data=b"\x89PNG_fake", content_type="image/png"):
    return client.put(
        f"/api/v1/hitos/{hito_id}/evidencia", headers=headers,
        files={"evidencia": (filename, data, content_type)},
    )


# --------------------------------------------------------------------------- #
# POST /{id}/aprobar
# --------------------------------------------------------------------------- #
def test_aprobar_sin_evidencia_409(client, db, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = client.post(f"/api/v1/hitos/{h.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 409
    assert r.json()["detail"] == SIN_EVIDENCIA
    db.refresh(h)
    assert h.estado == HITO_PENDIENTE
    assert h.aprobado_at is None


def test_aprobar_con_evidencia_200(client, db, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo, evidencia=True)
    r = client.post(f"/api/v1/hitos/{h.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert r.json()["estado"] == HITO_APROBADO


def test_reaprobar_rechazado_sin_evidencia_409(client, db, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo, estado=HITO_RECHAZADO)
    r = client.post(f"/api/v1/hitos/{h.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 409
    assert r.json()["detail"] == SIN_EVIDENCIA


# --------------------------------------------------------------------------- #
# POST /aprobar-lote
# --------------------------------------------------------------------------- #
def test_aprobar_lote_solo_con_evidencia(client, db, admin, lawyer, tipo):
    con = [_hito(db, lawyer, tipo, evidencia=True).id for _ in range(2)]
    sin = [_hito(db, lawyer, tipo).id for _ in range(2)]
    r = client.post("/api/v1/hitos/aprobar-lote", headers=_h(ADMIN_RUT), json={"ids": con + sin})
    assert r.status_code == 200
    body = r.json()
    assert body["procesados"] == 2
    assert sorted(body["ids"]) == sorted(con)
    assert body["sin_evidencia"] == 2
    assert sorted(body["omitidos_ids"]) == sorted(sin)
    for i in con:
        assert db.get(Hito, i).estado == HITO_APROBADO
    for i in sin:
        assert db.get(Hito, i).estado == HITO_PENDIENTE


def test_aprobar_lote_todos_sin_evidencia_no_aprueba_nada(client, db, admin, lawyer, tipo):
    sin = [_hito(db, lawyer, tipo).id for _ in range(3)]
    r = client.post("/api/v1/hitos/aprobar-lote", headers=_h(ADMIN_RUT), json={"ids": sin})
    assert r.status_code == 200
    body = r.json()
    assert body["procesados"] == 0 and body["ids"] == []
    assert body["sin_evidencia"] == 3
    assert sorted(body["omitidos_ids"]) == sorted(sin)


def test_rechazar_lote_no_reporta_omitidos(client, db, admin, lawyer, tipo):
    """The extended shape keeps the old fields and defaults the new ones for the other bulk actions."""
    ids = [_hito(db, lawyer, tipo).id]
    r = client.post("/api/v1/hitos/rechazar-lote", headers=_h(ADMIN_RUT), json={"ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert body["procesados"] == 1
    assert body["sin_evidencia"] == 0 and body["omitidos_ids"] == []


# --------------------------------------------------------------------------- #
# PUT /{id}/evidencia
# --------------------------------------------------------------------------- #
def test_put_evidencia_owner_pendiente_then_aprobar(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = _put(client, _h(LAWYER_RUT), h.id)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == h.id
    assert body["tiene_evidencia"] is True
    assert body["estado"] == HITO_PENDIENTE

    db.refresh(h)
    assert h.evidencia_storage_key == f"hitos/evidencia/{lawyer.id}/" + h.evidencia_storage_key.rsplit("/", 1)[1]
    assert h.evidencia_storage_key.endswith(".png")
    assert h.evidencia_filename == "cap.png"
    assert h.evidencia_content_type == "image/png"
    assert len(storage.uploads) == 1
    assert storage.uploads[0][2] == "image/png"

    r = client.post(f"/api/v1/hitos/{h.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 200
    assert r.json()["estado"] == HITO_APROBADO


def test_put_evidencia_replaces_existing(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo, estado=HITO_RECHAZADO, evidencia=True)
    old_key = h.evidencia_storage_key
    r = _put(client, _h(LAWYER_RUT), h.id, filename="nueva.pdf", data=b"%PDF-1.4 fake", content_type="application/pdf")
    assert r.status_code == 200
    db.refresh(h)
    assert h.evidencia_storage_key != old_key
    assert h.evidencia_storage_key.endswith(".pdf")
    assert h.evidencia_filename == "nueva.pdf"
    assert h.evidencia_content_type == "application/pdf"


def test_put_evidencia_admin_on_other_lawyer_hito(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = _put(client, _h(ADMIN_RUT), h.id)
    assert r.status_code == 200
    assert r.json()["tiene_evidencia"] is True
    # Key is scoped to the OWNING lawyer, not the admin who uploaded it.
    assert storage.uploads[0][0].startswith(f"hitos/evidencia/{lawyer.id}/")


def test_put_evidencia_other_lawyer_403(client, db, storage, admin, lawyer, other_lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = _put(client, _h(OTHER_RUT), h.id)
    assert r.status_code == 403
    assert r.json()["detail"] == "Sin acceso a esta evidencia"
    db.refresh(h)
    assert h.evidencia_storage_key is None
    assert storage.uploads == []


def test_put_evidencia_on_aprobado_409(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo, estado=HITO_APROBADO, evidencia=True)
    old_key = h.evidencia_storage_key
    r = _put(client, _h(ADMIN_RUT), h.id)
    assert r.status_code == 409
    assert r.json()["detail"] == ESTADO_NO_PERMITE
    db.refresh(h)
    assert h.evidencia_storage_key == old_key
    assert storage.uploads == []


def test_put_evidencia_invalid_content_type_415(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = _put(client, _h(LAWYER_RUT), h.id, filename="cap.gif", data=b"GIF89a", content_type="image/gif")
    assert r.status_code == 415
    assert r.json()["detail"] == "Formato de evidencia no permitido (usa PNG, JPG, WEBP o PDF)"
    assert storage.uploads == []


def test_put_evidencia_empty_file_422(client, db, storage, admin, lawyer, tipo):
    h = _hito(db, lawyer, tipo)
    r = _put(client, _h(LAWYER_RUT), h.id, data=b"")
    assert r.status_code == 422
    assert storage.uploads == []


def test_put_evidencia_not_found_404(client, db, storage, admin):
    r = _put(client, _h(ADMIN_RUT), 999999)
    assert r.status_code == 404
