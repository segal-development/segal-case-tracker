"""Tests for the external read-only Sysgal API slice 2:

- GET /api/sysgal/v1/plazos
- GET /api/sysgal/v1/novedades
- GET /api/sysgal/v1/buscar

Covers auth (X-API-Key), the required cliente_rut param (and buscar's q),
active-only + client-only scoping, bounded field sets (no internal id/lawyer
leak), the plazos window, novedades newest-first ordering, and buscar term
matching. Uses SQLite via the shared `db`/`client` conftest fixtures.
"""

import hashlib
from datetime import date, datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.document import Document
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_api_key import SysgalApiKey
from app.services.deadline_engine import _today_chile

VALID_KEY = "sysgal-valid-key"
CLIENT_RUT = "18765432-1"
OTHER_RUT = "9111111-1"

PLAZOS_URL = "/api/sysgal/v1/plazos"
NOVEDADES_URL = "/api/sysgal/v1/novedades"
BUSCAR_URL = "/api/sysgal/v1/buscar"


def _seed_key(db, plaintext: str, *, is_active: bool = True, revoked: bool = False):
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


def _auth():
    return {"X-API-Key": VALID_KEY}


@pytest.fixture
def seeded(db):
    """Firm lawyer + court + cases, litigantes, movements and documents.

    Cases:
      - case_active:    ACTIVE, client is DDO.  -> in scope
      - case_archived:  ARCHIVED, same client   -> excluded
      - case_other:     ACTIVE, only OTHER_RUT  -> excluded
    """
    today = _today_chile()

    lawyer = Lawyer(rut="16021492-9", name="Firm")
    court = Court(code="T1", name="1º Juzgado Civil de Santiago", region="RM", type="civil")
    other_court = Court(code="T2", name="2º Juzgado Civil de Santiago", region="RM", type="civil")
    db.add_all([lawyer, court, other_court])
    db.commit()
    db.refresh(lawyer)
    db.refresh(court)
    db.refresh(other_court)

    case_active = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-100-2025", status="active",
        competencia="civil", plaintiff="BANCO X", defendant="PEREZ",
        matter="Cobro de pesos", semaforo="rojo",
        next_deadline_at=today + timedelta(days=5), next_deadline_fatal=True,
    )
    case_archived = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-200-2024", status="archived",
        competencia="civil", plaintiff="BANCO Y", defendant="PEREZ",
        semaforo="rojo", next_deadline_at=today + timedelta(days=3),
    )
    case_other = Case(
        lawyer_id=lawyer.id, court_id=other_court.id, rol="C-300-2025", status="active",
        competencia="civil", plaintiff="BANCO Z", defendant="SOTO",
        semaforo="rojo", next_deadline_at=today + timedelta(days=2),
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

    # Movements: two on the active client case (different dates), one on the
    # archived case and one on the other-client case (both must be excluded).
    db.add_all([
        Movement(case_id=case_active.id, stage="Discusión", procedure="Notificación",
                 description="Se notifica la demanda al ejecutado.",
                 movement_date=datetime.combine(today - timedelta(days=1), datetime.min.time())),
        Movement(case_id=case_active.id, stage="Discusión", procedure="Ingreso",
                 description="Ingreso de demanda ejecutiva.",
                 movement_date=datetime.combine(today - timedelta(days=10), datetime.min.time())),
        Movement(case_id=case_active.id, stage="Vieja", procedure="Antigua",
                 description="Actuación muy antigua, fuera de ventana.",
                 movement_date=datetime.combine(today - timedelta(days=90), datetime.min.time())),
        Movement(case_id=case_archived.id, stage="X", procedure="Y",
                 description="Movimiento de causa archivada.",
                 movement_date=datetime.combine(today - timedelta(days=1), datetime.min.time())),
        Movement(case_id=case_other.id, stage="X", procedure="Y",
                 description="Movimiento de otro cliente.",
                 movement_date=datetime.combine(today - timedelta(days=1), datetime.min.time())),
    ])

    # Documents (with texto): one on the active client case matching "embargo",
    # one on archived and one on other-client that also mention embargo but must
    # be excluded by scope.
    db.add_all([
        Document(case_id=case_active.id, doc_type="resolution", status="stored",
                 texto="Resolución que decreta el embargo de bienes del ejecutado.",
                 document_date=datetime.combine(today - timedelta(days=1), datetime.min.time())),
        Document(case_id=case_active.id, doc_type="escrito", status="stored",
                 texto="Escrito de oposición de excepciones sin mención relevante."),
        Document(case_id=case_archived.id, doc_type="resolution", status="stored",
                 texto="Embargo dictado en causa archivada."),
        Document(case_id=case_other.id, doc_type="resolution", status="stored",
                 texto="Embargo dictado en causa de otro cliente."),
    ])
    db.commit()

    return {"case_active": case_active, "court": court}


# --------------------------------------------------------------------------- auth

@pytest.mark.parametrize("url", [PLAZOS_URL, NOVEDADES_URL, BUSCAR_URL])
def test_missing_api_key_returns_401(client, seeded, url):
    params = {"cliente_rut": CLIENT_RUT}
    if url == BUSCAR_URL:
        params["q"] = "embargo"
    resp = client.get(url, params=params)
    assert resp.status_code == 401


@pytest.mark.parametrize("url", [PLAZOS_URL, NOVEDADES_URL, BUSCAR_URL])
def test_wrong_api_key_returns_401(client, db, seeded, url):
    _seed_key(db, VALID_KEY)
    params = {"cliente_rut": CLIENT_RUT}
    if url == BUSCAR_URL:
        params["q"] = "embargo"
    resp = client.get(url, params=params, headers={"X-API-Key": "totally-wrong"})
    assert resp.status_code == 401


# ------------------------------------------------------------------ required params

def test_plazos_missing_cliente_rut_returns_422(client, db, seeded):
    _seed_key(db, VALID_KEY)
    assert client.get(PLAZOS_URL, headers=_auth()).status_code == 422


def test_novedades_missing_cliente_rut_returns_422(client, db, seeded):
    _seed_key(db, VALID_KEY)
    assert client.get(NOVEDADES_URL, headers=_auth()).status_code == 422


def test_buscar_missing_cliente_rut_returns_422(client, db, seeded):
    _seed_key(db, VALID_KEY)
    assert client.get(BUSCAR_URL, params={"q": "embargo"}, headers=_auth()).status_code == 422


def test_buscar_missing_q_returns_422(client, db, seeded):
    _seed_key(db, VALID_KEY)
    assert client.get(BUSCAR_URL, params={"cliente_rut": CLIENT_RUT}, headers=_auth()).status_code == 422


def test_buscar_short_q_returns_422(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(BUSCAR_URL, params={"cliente_rut": CLIENT_RUT, "q": "a"}, headers=_auth())
    assert resp.status_code == 422


# ------------------------------------------------------------------------- plazos

def test_plazos_returns_only_active_client_case(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(PLAZOS_URL, params={"cliente_rut": CLIENT_RUT}, headers=_auth())
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["rol"] == "C-100-2025"
    assert item["fatal"] is True
    assert item["overdue"] is False
    assert item["tribunal"] == "1º Juzgado Civil de Santiago"


def test_plazos_bounded_fields_only(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(PLAZOS_URL, params={"cliente_rut": CLIENT_RUT}, headers=_auth())
    item = resp.json()[0]
    assert set(item.keys()) == {
        "rol", "caratulado", "proximo_plazo", "fatal", "overdue", "semaforo", "tribunal",
    }
    for leaked in ("id", "case_id", "lawyer_id", "court_id"):
        assert leaked not in item


def test_plazos_respects_window(client, db, seeded):
    """A deadline beyond the horizon window is excluded."""
    _seed_key(db, VALID_KEY)
    # active case deadline is today+5; a 3-day window must exclude it.
    resp = client.get(
        PLAZOS_URL, params={"cliente_rut": CLIENT_RUT, "days": 3}, headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_plazos_other_client_empty(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(PLAZOS_URL, params={"cliente_rut": "99999999-9"}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == []


# ----------------------------------------------------------------------- novedades

def test_novedades_returns_only_active_client_movements_newest_first(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        NOVEDADES_URL, params={"cliente_rut": CLIENT_RUT, "days": 30}, headers=_auth()
    )
    assert resp.status_code == 200
    items = resp.json()
    # 2 movements in window on the active client case (the 90-day-old one and
    # the archived/other-client ones are excluded).
    assert len(items) == 2
    assert all(i["rol"] == "C-100-2025" for i in items)
    fechas = [i["fecha"] for i in items]
    assert fechas == sorted(fechas, reverse=True)  # newest first


def test_novedades_bounded_fields_only(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(NOVEDADES_URL, params={"cliente_rut": CLIENT_RUT}, headers=_auth())
    item = resp.json()[0]
    assert set(item.keys()) == {"rol", "fecha", "etapa", "tramite", "descripcion", "tribunal"}
    for leaked in ("id", "case_id", "lawyer_id", "court_id", "folio", "document_url"):
        assert leaked not in item


def test_novedades_limit_capped(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        NOVEDADES_URL, params={"cliente_rut": CLIENT_RUT, "limit": 1}, headers=_auth()
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# -------------------------------------------------------------------------- buscar

def test_buscar_matches_term_scoped_to_client_active_cases(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"cliente_rut": CLIENT_RUT, "q": "embargo"}, headers=_auth()
    )
    assert resp.status_code == 200
    items = resp.json()
    # Only the active client case's document mentions embargo in scope.
    assert len(items) == 1
    item = items[0]
    assert item["rol"] == "C-100-2025"
    assert "embargo" in item["snippet"].lower()
    assert item["doc_type"] == "resolution"


def test_buscar_bounded_fields_only(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"cliente_rut": CLIENT_RUT, "q": "embargo"}, headers=_auth()
    )
    item = resp.json()[0]
    assert set(item.keys()) == {"rol", "doc_type", "fecha", "snippet", "tribunal"}
    for leaked in ("document_id", "id", "case_id", "download_url", "filename"):
        assert leaked not in item


def test_buscar_no_match_returns_empty(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"cliente_rut": CLIENT_RUT, "q": "inexistente"}, headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_buscar_other_client_empty(client, db, seeded):
    _seed_key(db, VALID_KEY)
    resp = client.get(
        BUSCAR_URL, params={"cliente_rut": "99999999-9", "q": "embargo"}, headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.json() == []
