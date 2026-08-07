"""Tests for the external read-only Sysgal API: GET /api/sysgal/v1/causas.

Covers auth (X-API-Key), client-RUT filtering, active-only visibility, the
bounded field set, and the required cliente_rut param. Uses SQLite via the
shared `db`/`client` conftest fixtures.
"""

import hashlib

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_api_key import SysgalApiKey

VALID_KEY = "sysgal-valid-key"
CLIENT_RUT = "18765432-1"
OTHER_RUT = "9111111-1"

CAUSAS_URL = "/api/sysgal/v1/causas"


def _seed_key(db, plaintext: str, *, is_active: bool = True, revoked: bool = False):
    from datetime import datetime

    key = SysgalApiKey(
        label="sysgal-test",
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        is_active=is_active,
        revoked_at=datetime.utcnow() if revoked else None,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


@pytest.fixture
def seeded(db):
    """Firm lawyer + court + three cases with litigantes.

    - case_active_client:   ACTIVE, client (CLIENT_RUT) is DDO. -> visible
    - case_archived_client: ARCHIVED, same client -> excluded
    - case_active_other:    ACTIVE, only OTHER_RUT litigante -> excluded
    """
    lawyer = Lawyer(rut="16021492-9", name="Firm")
    court = Court(code="T1", name="1º Juzgado Civil de Santiago", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    db.refresh(lawyer)
    db.refresh(court)

    from datetime import date

    case_active = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-100-2025", status="active",
        competencia="civil", plaintiff="BANCO X", defendant="PEREZ",
        matter="Cobro de pesos", semaforo="verde", next_deadline_at=date(2026, 9, 1),
    )
    case_archived = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-200-2024", status="archived",
        competencia="civil", plaintiff="BANCO Y", defendant="PEREZ",
    )
    case_other = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-300-2025", status="active",
        competencia="civil", plaintiff="BANCO Z", defendant="SOTO",
    )
    db.add_all([case_active, case_archived, case_other])
    db.commit()
    for c in (case_active, case_archived, case_other):
        db.refresh(c)

    db.add_all([
        CaseLitigante(case_id=case_active.id, participante="DDO.", rut=CLIENT_RUT,
                      persona_type="NATURAL", nombre="JUAN PEREZ", natural_key="k1"),
        CaseLitigante(case_id=case_archived.id, participante="DDO.", rut=CLIENT_RUT,
                      persona_type="NATURAL", nombre="JUAN PEREZ", natural_key="k2"),
        CaseLitigante(case_id=case_other.id, participante="DDO.", rut=OTHER_RUT,
                      persona_type="NATURAL", nombre="ANA SOTO", natural_key="k3"),
    ])
    db.commit()
    return {"lawyer": lawyer, "court": court, "case_active": case_active}


# --------------------------------------------------------------------------- auth


def test_missing_api_key_returns_401(client, seeded):
    resp = client.get(CAUSAS_URL, params={"cliente_rut": CLIENT_RUT})
    assert resp.status_code == 401


def test_wrong_api_key_returns_401(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": CLIENT_RUT},
        headers={"X-API-Key": "totally-wrong"},
    )
    assert resp.status_code == 401


def test_inactive_key_returns_401(client, db, seeded):
    _seed_key(db, VALID_KEY, is_active=False)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": CLIENT_RUT},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 401


def test_revoked_key_returns_401(client, db, seeded):
    _seed_key(db, VALID_KEY, revoked=True)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": CLIENT_RUT},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 401


# ----------------------------------------------------------------------- filtering


def test_valid_key_returns_only_active_client_causas(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": CLIENT_RUT},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["per_page"] == 50
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["rol"] == "C-100-2025"  # active client case only
    # archived same-client case and other-client case excluded


def test_rut_is_normalized(client, db, seeded):
    """A dotted/spaced RUT still matches the stored normalized rut."""
    _seed_key(db, VALID_KEY)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": "18.765.432-1"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_other_client_rut_returns_empty(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": "99999999-9"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []


def test_missing_cliente_rut_returns_422(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(CAUSAS_URL, headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 422


# ------------------------------------------------------------------- bounded fields


def test_response_exposes_only_bounded_fields(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        CAUSAS_URL, params={"cliente_rut": CLIENT_RUT},
        headers={"X-API-Key": VALID_KEY},
    )
    item = resp.json()["items"][0]
    assert set(item.keys()) == {
        "rol", "caratulado", "materia", "estado",
        "semaforo", "proximo_plazo", "tribunal", "competencia",
    }
    # No internal ids / lawyer attribution / credentials leaked
    for leaked in ("id", "case_id", "lawyer_id", "court_id", "client_id", "rut"):
        assert leaked not in item
    assert item["caratulado"] == "BANCO X/PEREZ"
    assert item["tribunal"] == "1º Juzgado Civil de Santiago"
    assert item["materia"] == "Cobro de pesos"
    assert item["semaforo"] == "verde"
