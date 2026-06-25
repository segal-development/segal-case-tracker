"""Tests for GET /api/v1/stats/public-overview — PUBLIC (no-auth) login marquee counts."""

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


def _seed_firm(db):
    law = Lawyer(rut="99999999-9", name="Firm Account")
    db.add(law)
    db.commit()
    db.refresh(law)
    court = Court(code="PO-1", name="Juzgado PO", region="RM", type="civil")
    db.add(court)
    db.commit()
    db.refresh(court)
    rows = [
        ("C-1-2025", "rojo"), ("C-2-2025", "rojo"),
        ("C-3-2025", "amarillo"),
        ("C-4-2025", "verde"), ("C-5-2025", "verde"), ("C-6-2025", "verde"),
        ("C-7-2025", None),  # sin semáforo → no cuenta en r/a/v
    ]
    for rol, sem in rows:
        db.add(Case(rol=rol, lawyer_id=law.id, court_id=court.id, competencia="civil", semaforo=sem))
    db.commit()
    return law


def test_public_overview_requires_no_auth_and_counts(client, db):
    """No auth header → 200, and returns aggregated rojo/amarillo/verde counts only."""
    _seed_firm(db)
    r = client.get("/api/v1/stats/public-overview")
    assert r.status_code == 200
    body = r.json()
    assert body == {"rojo": 2, "amarillo": 1, "verde": 3}
    # never leaks case-level or personal data
    assert set(body.keys()) == {"rojo", "amarillo", "verde"}


def test_public_overview_no_data_returns_zeros(client, db):
    """No firm/cases → safe zeros, never an error (login must never break)."""
    r = client.get("/api/v1/stats/public-overview")
    assert r.status_code == 200
    assert r.json() == {"rojo": 0, "amarillo": 0, "verde": 0}
