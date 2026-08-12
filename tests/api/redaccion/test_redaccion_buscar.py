"""Tests for the external read-only Redaccion API: GET /api/redaccion/v1/buscar.

Covers auth (X-API-Key), search by ROL/RIT, search by client RUT, the
"at least one param" 400 rule, and the bounded field set. SQLite via the shared
`db`/`client` conftest fixtures.
"""

import hashlib
from datetime import date, datetime

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.redaccion_api_key import RedaccionApiKey

VALID_KEY = "redaccion-valid-key"
CLIENT_RUT = "18765432-1"
OTHER_RUT = "9111111-1"

BUSCAR_URL = "/api/redaccion/v1/buscar"


def _seed_key(db, plaintext: str, *, is_active: bool = True, revoked: bool = False):
    key = RedaccionApiKey(
        label="redaccion-test",
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
    """Firm lawyer + court + two cases with litigantes."""
    lawyer = Lawyer(rut="16021492-9", name="Firm")
    court = Court(code="T1", name="1º Juzgado Civil de Santiago", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    db.refresh(lawyer)
    db.refresh(court)

    case_a = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-100-2025", rit="RIT-777",
        status="active", competencia="civil", plaintiff="BANCO X", defendant="PEREZ",
        matter="Cobro de pesos", procedure="Ejecutivo", semaforo="verde",
        next_deadline_at=date(2026, 9, 1),
    )
    case_b = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-300-2025", status="active",
        competencia="civil", plaintiff="BANCO Z", defendant="SOTO",
    )
    db.add_all([case_a, case_b])
    db.commit()
    db.refresh(case_a)
    db.refresh(case_b)

    db.add_all([
        CaseLitigante(case_id=case_a.id, participante="DDO.", rut=CLIENT_RUT,
                      persona_type="NATURAL", nombre="JUAN PEREZ", natural_key="k1"),
        CaseLitigante(case_id=case_b.id, participante="DDO.", rut=OTHER_RUT,
                      persona_type="NATURAL", nombre="ANA SOTO", natural_key="k2"),
    ])
    db.commit()
    return {"lawyer": lawyer, "court": court, "case_a": case_a, "case_b": case_b}


# --------------------------------------------------------------------------- auth


def test_missing_api_key_returns_401(client, seeded):
    resp = client.get(BUSCAR_URL, params={"rol": "C-100"})
    assert resp.status_code == 401


def test_wrong_api_key_returns_401(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rol": "C-100"},
        headers={"X-API-Key": "totally-wrong"},
    )
    assert resp.status_code == 401


def test_inactive_key_returns_401(client, db, seeded):
    _seed_key(db, VALID_KEY, is_active=False)
    resp = client.get(
        BUSCAR_URL, params={"rol": "C-100"}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 401


def test_revoked_key_returns_401(client, db, seeded):
    _seed_key(db, VALID_KEY, revoked=True)
    resp = client.get(
        BUSCAR_URL, params={"rol": "C-100"}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 401


# ------------------------------------------------------------------- validation


def test_no_params_returns_400(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(BUSCAR_URL, headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 400


def test_blank_params_returns_400(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rol": "  ", "rut": ""}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------- searching


def test_buscar_by_rol(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rol": "c-100"}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["rol"] == "C-100-2025"
    assert item["rit"] == "RIT-777"
    assert item["case_id"] == seeded["case_a"].id


def test_buscar_by_rit(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rol": "RIT-777"}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_buscar_by_rut_normalized(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rut": "18.765.432-1"}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["case_id"] == seeded["case_a"].id


def test_buscar_by_rut_no_match(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rut": "99999999-9"}, headers={"X-API-Key": VALID_KEY}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_buscar_bounded_fields(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"rol": "C-100"}, headers={"X-API-Key": VALID_KEY}
    )
    item = resp.json()["items"][0]
    assert set(item.keys()) == {
        "case_id", "rol", "rit", "caratulado", "materia", "procedimiento",
        "tribunal", "estado", "semaforo", "proximo_plazo",
    }
    assert item["caratulado"] == "BANCO X/PEREZ"
    assert item["tribunal"] == "1º Juzgado Civil de Santiago"
    assert item["materia"] == "Cobro de pesos"
    assert item["procedimiento"] == "Ejecutivo"
