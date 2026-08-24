"""Tests for the external Presentación (GEDOC escrito-upload) API.

POST /api/presentacion/v1/presentaciones + GET /{id} — covers auth, queueing a
filing (estado="en_cola"), idempotency (a repeat POST returns the same row), the
modo/tipo_gestion validators, and the status lookup / 404. Slice 1 only: no OJV
or PJUD writes are involved. SQLite via the shared conftest fixtures.
"""

import hashlib

from app.models.presentacion import Presentacion
from app.models.presentacion_api_key import PresentacionApiKey

VALID_KEY = "presentacion-valid-key"
POST_URL = "/api/presentacion/v1/presentaciones"


def _seed_key(db, plaintext: str = VALID_KEY):
    key = PresentacionApiKey(
        label="presentacion-test",
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        is_active=True,
    )
    db.add(key)
    db.commit()
    return key


def _body(**overrides) -> dict:
    body = {
        "idempotency_key": "gedoc-abc-123",
        "tipo_gestion": "escrito",
        "credential_ref": "16021492-9",
        "modo": "semiauto",
        "tribunal": "1º Juzgado Civil de Santiago",
        "rol": "C-100-2025",
        "anio": "2025",
        "litigantes": [
            {"tipo_sujeto": "DDO.", "rut": "18765432-1", "tipo_persona": "NATURAL",
             "nombres": "JUAN", "apellidos": "PEREZ"},
        ],
        "documento_principal": {"url": "https://gedoc/doc.pdf", "referencia": "Escrito"},
        "documentos": [
            {"url": "https://gedoc/anexo.pdf", "referencia": "Anexo 1", "tipo": "anexo",
             "cantidad": 1, "original_papel": False},
        ],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- auth


def test_post_missing_key_401(client, db):
    resp = client.post(POST_URL, json=_body())
    assert resp.status_code == 401


def test_post_wrong_key_401(client, db):
    _seed_key(db)
    resp = client.post(POST_URL, json=_body(), headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


# ------------------------------------------------------------------------ create


def test_post_creates_en_cola(client, db):
    _seed_key(db)
    resp = client.post(POST_URL, json=_body(), headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 202
    body = resp.json()
    assert body["estado"] == "en_cola"
    assert body["idempotency_key"] == "gedoc-abc-123"
    assert isinstance(body["id"], int)

    # payload persisted with the documents + litigantes.
    row = db.query(Presentacion).filter(Presentacion.id == body["id"]).first()
    assert row is not None
    assert row.payload["documento_principal"]["url"] == "https://gedoc/doc.pdf"
    assert len(row.payload["litigantes"]) == 1
    assert len(row.payload["documentos"]) == 1


# --------------------------------------------------------------------- idempotency


def test_post_same_idempotency_key_returns_same_row(client, db):
    _seed_key(db)
    first = client.post(POST_URL, json=_body(), headers={"X-API-Key": VALID_KEY})
    assert first.status_code == 202
    first_id = first.json()["id"]

    second = client.post(POST_URL, json=_body(), headers={"X-API-Key": VALID_KEY})
    assert second.status_code == 202
    assert second.json()["id"] == first_id

    # exactly one row exists for that idempotency_key.
    rows = (
        db.query(Presentacion)
        .filter(Presentacion.idempotency_key == "gedoc-abc-123")
        .all()
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------- validators


def test_post_invalid_modo_422(client, db):
    _seed_key(db)
    resp = client.post(
        POST_URL, json=_body(modo="banana"), headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 422


def test_post_invalid_tipo_gestion_422(client, db):
    _seed_key(db)
    resp = client.post(
        POST_URL, json=_body(tipo_gestion="oficio"), headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 422


def test_post_empty_idempotency_key_422(client, db):
    _seed_key(db)
    resp = client.post(
        POST_URL, json=_body(idempotency_key="   "), headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------- get


def test_get_existing_200(client, db):
    _seed_key(db)
    created = client.post(POST_URL, json=_body(), headers={"X-API-Key": VALID_KEY})
    pres_id = created.json()["id"]

    resp = client.get(f"{POST_URL}/{pres_id}", headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == pres_id
    assert body["estado"] == "en_cola"
    assert body["tipo_gestion"] == "escrito"
    assert body["modo"] == "semiauto"
    assert body["numero_identificador"] is None


def test_get_missing_404(client, db):
    _seed_key(db)
    resp = client.get(f"{POST_URL}/999999", headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 404


def test_get_missing_key_401(client, db):
    _seed_key(db)
    resp = client.get(f"{POST_URL}/1")
    assert resp.status_code == 401


# --------------------------------------------------------------- revisar / enviar


def _seed_cargada(db, idempotency_key: str = "gedoc-enviar-1") -> Presentacion:
    """Seed a row directly at ``cargada_pendiente_envio`` (as the worker would)."""
    row = Presentacion(
        idempotency_key=idempotency_key,
        tipo_gestion="escrito",
        credential_ref="16021492-9",
        payload={"litigantes": [], "documento_principal": {}, "documentos": []},
        estado="cargada_pendiente_envio",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_revisado(
    db,
    idempotency_key: str = "gedoc-revisado-1",
    revisado_por: str = "revisor@segal.cl",
) -> Presentacion:
    """Seed a row directly at ``revisado`` (already cross-reviewed)."""
    from datetime import datetime

    row = Presentacion(
        idempotency_key=idempotency_key,
        tipo_gestion="escrito",
        credential_ref="16021492-9",
        payload={"litigantes": [], "documento_principal": {}, "documentos": []},
        estado="revisado",
        revisado_por=revisado_por,
        revisado_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ----------------------------------------------------------------------- revisar


def test_revisar_from_cargada_200(client, db):
    _seed_key(db)
    row = _seed_cargada(db)

    resp = client.post(
        f"{POST_URL}/{row.id}/revisar",
        json={"revisado_por": "revisor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["estado"] == "revisado"
    assert body["revisado_por"] == "revisor@segal.cl"
    assert body["revisado_at"] is not None


def test_revisar_is_idempotent(client, db):
    _seed_key(db)
    row = _seed_cargada(db)

    first = client.post(
        f"{POST_URL}/{row.id}/revisar",
        json={"revisado_por": "revisor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert first.status_code == 200

    # A second review (with a different revisado_por) is a no-op.
    second = client.post(
        f"{POST_URL}/{row.id}/revisar",
        json={"revisado_por": "otro@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["estado"] == "revisado"
    assert body["revisado_por"] == "revisor@segal.cl"  # unchanged


def test_revisar_from_en_cola_409(client, db):
    _seed_key(db)
    created = client.post(POST_URL, json=_body(), headers={"X-API-Key": VALID_KEY})
    pres_id = created.json()["id"]

    resp = client.post(
        f"{POST_URL}/{pres_id}/revisar",
        json={"revisado_por": "revisor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 409


def test_revisar_missing_404(client, db):
    _seed_key(db)
    resp = client.post(
        f"{POST_URL}/999999/revisar",
        json={"revisado_por": "revisor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 404


def test_revisar_missing_key_401(client, db):
    _seed_key(db)
    row = _seed_cargada(db)
    resp = client.post(
        f"{POST_URL}/{row.id}/revisar", json={"revisado_por": "x"}
    )
    assert resp.status_code == 401


def test_get_after_revisar_reflects_state(client, db):
    _seed_key(db)
    row = _seed_cargada(db)
    client.post(
        f"{POST_URL}/{row.id}/revisar",
        json={"revisado_por": "revisor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )

    resp = client.get(f"{POST_URL}/{row.id}", headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["estado"] == "revisado"
    assert body["revisado_por"] == "revisor@segal.cl"
    assert body["revisado_at"] is not None


# ------------------------------------------------------------------------ enviar


def test_enviar_from_cargada_requires_review_409(client, db):
    # A filing not yet cross-reviewed cannot be sent.
    _seed_key(db)
    row = _seed_cargada(db)

    resp = client.post(
        f"{POST_URL}/{row.id}/enviar",
        json={"confirmado_por": "redactor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 409
    # State is unchanged — still awaiting review.
    db.refresh(row)
    assert row.estado == "cargada_pendiente_envio"


def test_enviar_confirms_revisado_200(client, db):
    _seed_key(db)
    row = _seed_cargada(db)

    # Cross-review by one person...
    client.post(
        f"{POST_URL}/{row.id}/revisar",
        json={"revisado_por": "revisor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    # ...then a DIFFERENT person sends.
    resp = client.post(
        f"{POST_URL}/{row.id}/enviar",
        json={"confirmado_por": "redactor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["estado"] == "envio_confirmado"
    assert body["confirmado_por"] == "redactor@segal.cl"
    assert body["confirmado_at"] is not None


def test_enviar_four_eyes_same_person_409(client, db):
    # The sender must not be the same person who cross-reviewed.
    _seed_key(db)
    row = _seed_revisado(db, revisado_por="mismo@segal.cl")

    resp = client.post(
        f"{POST_URL}/{row.id}/enviar",
        json={"confirmado_por": "mismo@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 409
    # State unchanged — the send was rejected.
    db.refresh(row)
    assert row.estado == "revisado"


def test_enviar_is_idempotent(client, db):
    _seed_key(db)
    row = _seed_revisado(db)

    first = client.post(
        f"{POST_URL}/{row.id}/enviar",
        json={"confirmado_por": "redactor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert first.status_code == 200

    # A second confirm (with a different confirmado_por) is a no-op.
    second = client.post(
        f"{POST_URL}/{row.id}/enviar",
        json={"confirmado_por": "otro@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["estado"] == "envio_confirmado"
    assert body["confirmado_por"] == "redactor@segal.cl"  # unchanged


def test_enviar_from_en_cola_409(client, db):
    _seed_key(db)
    created = client.post(POST_URL, json=_body(), headers={"X-API-Key": VALID_KEY})
    pres_id = created.json()["id"]

    resp = client.post(
        f"{POST_URL}/{pres_id}/enviar",
        json={"confirmado_por": "redactor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 409


def test_enviar_missing_404(client, db):
    _seed_key(db)
    resp = client.post(
        f"{POST_URL}/999999/enviar",
        json={"confirmado_por": "redactor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 404


def test_enviar_missing_key_401(client, db):
    _seed_key(db)
    row = _seed_revisado(db)
    resp = client.post(
        f"{POST_URL}/{row.id}/enviar", json={"confirmado_por": "x"}
    )
    assert resp.status_code == 401


def test_get_after_enviar_reflects_state(client, db):
    _seed_key(db)
    row = _seed_revisado(db)
    client.post(
        f"{POST_URL}/{row.id}/enviar",
        json={"confirmado_por": "redactor@segal.cl"},
        headers={"X-API-Key": VALID_KEY},
    )

    resp = client.get(f"{POST_URL}/{row.id}", headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["estado"] == "envio_confirmado"
    assert body["confirmado_por"] == "redactor@segal.cl"
    assert body["confirmado_at"] is not None
