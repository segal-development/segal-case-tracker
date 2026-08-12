"""Tests for the external read-only Redaccion API: the detalle bundle.

GET /api/redaccion/v1/causas/{case_id}/detalle — covers auth, the full bundle
(identificación, estado, etapa, acción recomendada, plazos, partes, historial),
the 404 for a missing case, and the guarantee that NO documents/files are
exposed anywhere in the response. SQLite via the shared conftest fixtures.
"""

import hashlib
from datetime import date, datetime

import pytest

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.redaccion_api_key import RedaccionApiKey

VALID_KEY = "redaccion-valid-key"
CLIENT_RUT = "18765432-1"


def _detalle_url(case_id) -> str:
    return f"/api/redaccion/v1/causas/{case_id}/detalle"


def _seed_key(db, plaintext: str = VALID_KEY):
    key = RedaccionApiKey(
        label="redaccion-test",
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        is_active=True,
    )
    db.add(key)
    db.commit()
    return key


@pytest.fixture
def seeded_case(db):
    """A fully-populated case: litigantes + a deadline + movements + a
    recommended-action code so every detalle block has content."""
    lawyer = Lawyer(rut="16021492-9", name="Firm")
    court = Court(code="T1", name="1º Juzgado Civil de Santiago", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    db.refresh(lawyer)
    db.refresh(court)

    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-100-2025", rit="RIT-777",
        status="active", competencia="civil", plaintiff="BANCO X", defendant="PEREZ",
        matter="Cobro de pesos", procedure="Ejecutivo", semaforo="rojo",
        procedural_state="notificado", next_deadline_at=date(2026, 9, 1),
        next_deadline_fatal=True, recommended_action_code="oponer_excepciones",
        filed_at=datetime(2025, 1, 15),
    )
    db.add(case)
    db.commit()
    db.refresh(case)

    db.add_all([
        CaseLitigante(case_id=case.id, participante="DDO.", rut=CLIENT_RUT,
                      persona_type="NATURAL", nombre="JUAN PEREZ", natural_key="k1"),
        CaseLitigante(case_id=case.id, participante="DTE.", rut="70123456-7",
                      persona_type="JURIDICA", nombre="BANCO X", natural_key="k2"),
        CaseDeadline(case_id=case.id, deadline_type="excepciones_8d",
                     legal_basis="art. 459 CPC", due_date=date(2026, 9, 1),
                     triggered_at=date(2026, 8, 20), status="active"),
        Movement(case_id=case.id, stage="Notificación", procedure="Notifica demanda",
                 description="Se notifica la demanda ejecutiva.",
                 movement_date=datetime(2025, 2, 1)),
        Movement(case_id=case.id, stage="Mandamiento", procedure="Despacha",
                 description="Mandamiento de ejecución y embargo.",
                 movement_date=datetime(2025, 1, 20)),
    ])
    db.commit()
    return case


# --------------------------------------------------------------------------- auth


def test_detalle_missing_key_401(client, seeded_case):
    resp = client.get(_detalle_url(seeded_case.id))
    assert resp.status_code == 401


def test_detalle_wrong_key_401(client, db, seeded_case):
    _seed_key(db)
    resp = client.get(_detalle_url(seeded_case.id), headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- 404


def test_detalle_missing_case_404(client, db, seeded_case):
    _seed_key(db)
    resp = client.get(_detalle_url(999999), headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 404


# --------------------------------------------------------------------- full bundle


def test_detalle_full_bundle(client, db, seeded_case):
    _seed_key(db)
    resp = client.get(_detalle_url(seeded_case.id), headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 200
    body = resp.json()

    assert body["case_id"] == seeded_case.id

    ident = body["identificacion"]
    assert ident["rol"] == "C-100-2025"
    assert ident["rit"] == "RIT-777"
    assert ident["caratulado"] == "BANCO X/PEREZ"
    assert ident["materia"] == "Cobro de pesos"
    assert ident["procedimiento"] == "Ejecutivo"
    assert ident["tribunal"]["nombre"] == "1º Juzgado Civil de Santiago"

    estado = body["estado"]
    assert estado["status"] == "active"
    assert estado["semaforo"] == "rojo"
    assert estado["procedural_state"] == "notificado"

    etapa = body["etapa"]
    assert etapa["procedural_state"] == "notificado"
    assert etapa["etapa_label"] == "Demanda notificada"
    current = [s for s in etapa["flow"] if s["actual"]]
    assert len(current) == 1 and current[0]["code"] == "notificado"

    accion = body["accion_recomendada"]
    assert accion is not None
    assert accion["code"] == "oponer_excepciones"
    assert accion["legal_basis"]  # non-empty legal cite
    assert accion["disclaimer"]

    plazos = body["plazos"]
    assert plazos["next_deadline_fatal"] is True
    assert len(plazos["activos"]) == 1
    assert plazos["activos"][0]["deadline_type"] == "excepciones_8d"
    assert plazos["activos"][0]["label"]  # resolved human label
    assert "disclaimer" in plazos

    assert len(body["partes"]) == 2
    ruts = {p["rut"] for p in body["partes"]}
    assert CLIENT_RUT in ruts

    # historial = movements, most-recent-first
    hist = body["historial"]
    assert len(hist) == 2
    assert hist[0]["descripcion"] == "Se notifica la demanda ejecutiva."
    assert hist[0]["etapa"] == "Notificación"
    assert hist[0]["tramite"] == "Notifica demanda"


def test_detalle_exposes_no_documents(client, db, seeded_case):
    _seed_key(db)
    resp = client.get(_detalle_url(seeded_case.id), headers={"X-API-Key": VALID_KEY})
    body = resp.json()

    top_keys = set(body.keys())
    assert "documents" not in top_keys
    assert "documentos" not in top_keys

    # No document/download/file leakage anywhere in the serialized payload.
    import json

    blob = json.dumps(body).lower()
    for forbidden in ("download_url", "download", "storage_key", "gcs", "doc_token", "/documents/"):
        assert forbidden not in blob, f"leaked '{forbidden}'"
