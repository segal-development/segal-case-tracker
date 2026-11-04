"""POST /hitos for a plain lawyer follows the strict public-form rules.

A plain lawyer (role lawyer, no ``can_manage_evaluaciones``) no longer uses the
personal link, so the authenticated endpoint must enforce what the link did:
every field and the evidence are required and the hito is stamped ``formulario``.
Admins and lawyers with ``can_manage_evaluaciones`` keep the lenient behaviour.
"""
from datetime import date

import pytest

from app.core.security import create_access_token
from app.models.hito import Hito, HitoTipo, ORIGEN_FORMULARIO, ORIGEN_MANUAL
from app.models.lawyer import Lawyer
from app.services import storage_service

ADMIN_RUT = "16021492-9"
LAWYER_RUT = "19643548-4"
OTHER_RUT = "18248270-6"
MANAGER_RUT = "17654321-K"


class _FakeBackend:
    def __init__(self):
        self.uploads = []

    def upload(self, data, key, content_type="application/pdf"):
        self.uploads.append((key, data, content_type))
        return key

    def retrieve(self, key):
        return next(d for k, d, _ in self.uploads if k == key)


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(storage_service, "get_storage_backend", lambda *_a, **_k: backend)
    return backend


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
def lawyer(db):
    return _lawyer(db, LAWYER_RUT, "Benjamín Lawyer")


@pytest.fixture
def other(db):
    return _lawyer(db, OTHER_RUT, "Otra Abogada")


@pytest.fixture
def manager(db):
    return _lawyer(db, MANAGER_RUT, "Melissa", can_manage_evaluaciones=True)


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


def _full(tipo, **over):
    data = {
        "hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15", "rol_causa": "12.345.678-5",
        "descripcion": "C-1-2026", "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo",
    }
    data.update(over)
    return data


def _post(client, rut, data, evidencia=True):
    files = {"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")} if evidencia else None
    return client.post("/api/v1/hitos", headers=_h(rut), data=data, files=files)


def test_plain_lawyer_without_evidence_422(client, db, lawyer, tipo):
    r = _post(client, LAWYER_RUT, _full(tipo), evidencia=False)
    assert r.status_code == 422
    assert "evidencia" in r.json()["detail"].lower()
    assert db.query(Hito).count() == 0


@pytest.mark.parametrize("field", ["rol_causa", "descripcion", "tribunal", "procedimiento"])
def test_plain_lawyer_missing_field_422(client, db, lawyer, tipo, field):
    data = _full(tipo)
    del data[field]
    r = _post(client, LAWYER_RUT, data)
    assert r.status_code == 422
    assert r.json()["detail"] == f"El campo {field} es obligatorio"
    assert db.query(Hito).count() == 0


def test_plain_lawyer_blank_field_422(client, db, lawyer, tipo):
    r = _post(client, LAWYER_RUT, _full(tipo, tribunal="   "))
    assert r.status_code == 422
    assert r.json()["detail"] == "El campo tribunal es obligatorio"


def test_plain_lawyer_empty_evidence_422(client, db, lawyer, tipo):
    r = client.post(
        "/api/v1/hitos", headers=_h(LAWYER_RUT), data=_full(tipo),
        files={"evidencia": ("cap.png", b"", "image/png")},
    )
    assert r.status_code == 422
    assert db.query(Hito).count() == 0


def test_plain_lawyer_complete_201_formulario_with_evidence(client, db, lawyer, tipo):
    r = _post(client, LAWYER_RUT, _full(tipo))
    assert r.status_code == 201
    body = r.json()
    assert body["origen"] == ORIGEN_FORMULARIO
    assert body["tiene_evidencia"] is True
    assert body["lawyer_id"] == lawyer.id
    assert db.query(Hito).one().origen == ORIGEN_FORMULARIO


def test_plain_lawyer_length_checks_still_apply(client, db, lawyer, tipo):
    r = _post(client, LAWYER_RUT, _full(tipo, tribunal="x" * 500))
    assert r.status_code == 422
    assert db.query(Hito).count() == 0


def test_plain_lawyer_cannot_create_for_someone_else(client, db, lawyer, other, tipo):
    r = _post(client, LAWYER_RUT, _full(tipo, lawyer_id=other.id))
    assert r.status_code == 403
    assert db.query(Hito).count() == 0


def test_formulario_hito_of_lawyer_cannot_be_approved_without_evidence(client, db, admin, lawyer, tipo):
    h = Hito(
        lawyer_id=lawyer.id, hito_tipo_id=tipo.id, valor_bruto=8077,
        fecha_hito=date(2026, 7, 15), origen=ORIGEN_FORMULARIO,
    )
    db.add(h)
    db.commit()
    r = client.post(f"/api/v1/hitos/{h.id}/aprobar", headers=_h(ADMIN_RUT))
    assert r.status_code == 409


def test_admin_without_evidence_still_201_manual(client, db, admin, tipo):
    r = _post(client, ADMIN_RUT, {"hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15"}, evidencia=False)
    assert r.status_code == 201
    assert r.json()["origen"] == ORIGEN_MANUAL
    assert r.json()["tiene_evidencia"] is False


def test_lawyer_with_can_manage_evaluaciones_unchanged(client, db, manager, tipo):
    r = _post(client, MANAGER_RUT, {"hito_tipo_id": tipo.id, "fecha_hito": "2026-07-15"}, evidencia=False)
    assert r.status_code == 201
    assert r.json()["origen"] == ORIGEN_MANUAL
    assert r.json()["tiene_evidencia"] is False
