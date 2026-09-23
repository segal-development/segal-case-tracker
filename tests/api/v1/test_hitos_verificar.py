"""'Does this hito already exist?' preview for the public hito forms.

Read-only GET counterparts to the public POST routes (``/public/{token}`` and
``/public-procuradores/{token}``): given the three fields that identify a
causa (RUT cliente, ROL, tribunal), answer BEFORE the whole form — evidence
included — is filled in, instead of only on submit. A procurador who fills
the entire shared form and only then hits a 409 for a hito the lawyer already
filed weeks earlier escalates it instead of just picking another causa.

Both endpoints MUST reuse ``_hito_existente``, the exact same helper
``_create_hito`` calls to raise the 409 — a preview that can drift from the
rule it previews is worse than no preview at all.
"""
from datetime import date

import pytest

from app.models.hito import Hito, HitoTipo
from app.models.hito_form_link import FORM_LINK_KIND_PROCURADORES, HitoFormLink
from app.models.lawyer import Lawyer
from app.services import storage_service

LAWYER_RUT = "19643548-4"
OTHER_RUT = "18248270-6"
TOKEN = "tok-benjamin-0123456789"
PROC_TOKEN = "tok-procuradores-0123456789"

LINK_INVALIDO = "Link inválido o vencido"
ABOGADO_INVALIDO = "Selecciona un abogado válido"


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
def lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Benjamín Lawyer", role="lawyer", nivel="pleno",
                 hito_form_token=TOKEN)
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
def proc_link(db):
    row = HitoFormLink(kind=FORM_LINK_KIND_PROCURADORES, token=PROC_TOKEN)
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


def _existente(db, lawyer_id, tipo_id, *, rol_causa="C-1-2026", descripcion="C-100-2026",
                tribunal="1º Juzgado Civil", estado="pendiente", created_by_name=None):
    """A hito already loaded directly in the DB (bypassing the form), so tests
    control exactly what it stores (e.g. a blank tribunal)."""
    h = Hito(
        lawyer_id=lawyer_id, hito_tipo_id=tipo_id, valor_bruto=8077,
        fecha_hito=date(2026, 7, 1), rol_causa=rol_causa, descripcion=descripcion,
        tribunal=tribunal, estado=estado, created_by_name=created_by_name,
    )
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def _verificar(client, token, **params):
    return client.get(f"/api/v1/hitos/public/{token}/verificar", params=params)


def _verificar_proc(client, token, **params):
    return client.get(f"/api/v1/hitos/public-procuradores/{token}/verificar", params=params)


def _form(tipo_id, **over):
    data = {
        "hito_tipo_id": tipo_id, "fecha_hito": "2026-07-15", "rol_causa": "C-1-2026",
        "descripcion": "C-100-2026", "tribunal": "1º Juzgado Civil", "procedimiento": "Ejecutivo",
    }
    data.update(over)
    return data


def _post_public(client, token, tipo_id, evidencia=True, extra=None):
    data = _form(tipo_id, **(extra or {}))
    files = {"evidencia": ("cap.png", b"\x89PNG_fake", "image/png")} if evidencia else None
    return client.post(f"/api/v1/hitos/public/{token}", data=data, files=files)


# --------------------------------------------------------------------------- #
# No collision → existe: false
# --------------------------------------------------------------------------- #
def test_no_existing_hito_existe_false(client, db, storage, lawyer, tipo):
    r = _verificar(client, TOKEN, rol_causa="C-1-2026", descripcion="C-100-2026",
                    tribunal="1º Juzgado Civil")
    assert r.status_code == 200
    assert r.json() == {"existe": False, "detalle": None}


# --------------------------------------------------------------------------- #
# Collision → existe: true, detalle == the POST's 409 for the SAME inputs
# --------------------------------------------------------------------------- #
def test_existing_hito_existe_true_matches_post_409(client, db, storage, lawyer, tipo):
    assert _post_public(client, TOKEN, tipo.id).status_code == 201  # first one goes through

    r = _verificar(client, TOKEN, rol_causa="C-1-2026", descripcion="C-100-2026",
                    tribunal="1º Juzgado Civil")
    assert r.status_code == 200
    body = r.json()
    assert body["existe"] is True
    assert body["detalle"]

    post = _post_public(client, TOKEN, tipo.id)  # same inputs the preview just checked
    assert post.status_code == 409
    assert post.json()["detail"] == body["detalle"]  # preview and rejection agree, byte for byte


# --------------------------------------------------------------------------- #
# Tolerant tribunal rule (both directions) — see _tribunal_collides
# --------------------------------------------------------------------------- #
def test_blank_stored_tribunal_collides_with_query_tribunal(client, db, storage, lawyer, tipo):
    _existente(db, lawyer.id, tipo.id, tribunal=None)
    r = _verificar(client, TOKEN, rol_causa="C-1-2026", descripcion="C-100-2026",
                    tribunal="1º Juzgado Civil")
    assert r.json()["existe"] is True


def test_blank_query_tribunal_collides_with_stored_tribunal(client, db, storage, lawyer, tipo):
    _existente(db, lawyer.id, tipo.id, tribunal="1º Juzgado Civil")
    r = _verificar(client, TOKEN, rol_causa="C-1-2026", descripcion="C-100-2026")  # no tribunal
    assert r.json()["existe"] is True


# --------------------------------------------------------------------------- #
# A different lawyer's hito never collides (procuradores route)
# --------------------------------------------------------------------------- #
def test_procuradores_different_lawyer_does_not_collide(
    client, db, storage, lawyer, other_lawyer, proc_link, tipo,
):
    _existente(db, lawyer.id, tipo.id)  # belongs to `lawyer`, not `other_lawyer`
    r = _verificar_proc(client, PROC_TOKEN, lawyer_id=other_lawyer.id, rol_causa="C-1-2026",
                         descripcion="C-100-2026", tribunal="1º Juzgado Civil")
    assert r.status_code == 200
    assert r.json() == {"existe": False, "detalle": None}


def test_procuradores_same_lawyer_does_collide(client, db, storage, lawyer, proc_link, tipo):
    _existente(db, lawyer.id, tipo.id)
    r = _verificar_proc(client, PROC_TOKEN, lawyer_id=lawyer.id, rol_causa="C-1-2026",
                         descripcion="C-100-2026", tribunal="1º Juzgado Civil")
    assert r.status_code == 200
    assert r.json()["existe"] is True


# --------------------------------------------------------------------------- #
# Missing rol_causa → the create path does not dedup at all, so neither does this
# --------------------------------------------------------------------------- #
def test_missing_rol_causa_existe_false(client, db, storage, lawyer, tipo):
    _existente(db, lawyer.id, tipo.id)
    r = _verificar(client, TOKEN, descripcion="C-100-2026", tribunal="1º Juzgado Civil")
    assert r.status_code == 200
    assert r.json() == {"existe": False, "detalle": None}


# --------------------------------------------------------------------------- #
# Invalid token → same 404 as every other public route
# --------------------------------------------------------------------------- #
def test_invalid_token_404_per_lawyer(client, db):
    r = _verificar(client, "nope", rol_causa="C-1-2026")
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO


def test_invalid_token_404_procuradores(client, db, other_lawyer):
    r = _verificar_proc(client, "nope", lawyer_id=other_lawyer.id, rol_causa="C-1-2026")
    assert r.status_code == 404
    assert r.json()["detail"] == LINK_INVALIDO


# --------------------------------------------------------------------------- #
# Invalid lawyer_id (procuradores route) → same 422 as the POST
# --------------------------------------------------------------------------- #
def test_missing_lawyer_id_422(client, db, proc_link):
    r = _verificar_proc(client, PROC_TOKEN, rol_causa="C-1-2026")  # no lawyer_id at all
    assert r.status_code == 422
    assert r.json()["detail"] == ABOGADO_INVALIDO


def test_unknown_lawyer_id_422(client, db, proc_link):
    r = _verificar_proc(client, PROC_TOKEN, lawyer_id=999999, rol_causa="C-1-2026")
    assert r.status_code == 422
    assert r.json()["detail"] == ABOGADO_INVALIDO


def test_inactive_or_non_firm_lawyer_id_422(client, db, proc_link):
    inactive = Lawyer(rut="11111111-1", name="Inactiva", role="lawyer", is_active=False)
    admin = Lawyer(rut="16021492-9", name="Carla Admin", role="admin", is_firm_lawyer=False)
    db.add_all([inactive, admin])
    db.commit()
    db.refresh(inactive)
    db.refresh(admin)
    for lawyer_id in (inactive.id, admin.id):
        r = _verificar_proc(client, PROC_TOKEN, lawyer_id=lawyer_id, rol_causa="C-1-2026")
        assert r.status_code == 422
        assert r.json()["detail"] == ABOGADO_INVALIDO


# --------------------------------------------------------------------------- #
# Route is not shadowed by /{hito_id}/... — it must actually resolve here
# --------------------------------------------------------------------------- #
def test_route_not_shadowed_by_hito_id_per_lawyer(client, db, lawyer):
    r = _verificar(client, TOKEN)  # no query params at all — still hits OUR handler
    assert r.status_code == 200
    assert set(r.json()) == {"existe", "detalle"}


def test_route_not_shadowed_by_hito_id_procuradores(client, db, other_lawyer, proc_link):
    r = _verificar_proc(client, PROC_TOKEN, lawyer_id=other_lawyer.id)
    assert r.status_code == 200
    assert set(r.json()) == {"existe", "detalle"}


# --------------------------------------------------------------------------- #
# Nothing beyond existe/detalle leaks through a shared, unauthenticated link
# --------------------------------------------------------------------------- #
def test_response_shape_is_minimal(client, db, storage, lawyer, tipo):
    _existente(db, lawyer.id, tipo.id, created_by_name="CARLA PATRICIA LAVÍN BENITO")
    r = _verificar(client, TOKEN, rol_causa="C-1-2026", descripcion="C-100-2026",
                   tribunal="1º Juzgado Civil")
    assert set(r.json()) == {"existe", "detalle"}
