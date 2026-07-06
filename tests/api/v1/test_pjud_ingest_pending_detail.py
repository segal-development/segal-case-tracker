"""Tests for GET /api/v1/pjud/ingest/pending-detail.

Auth is via the X-Ingest-Key header (require_ingest_key), same as the
cases ingest endpoint. Returns the stalest cases (by ``last_detail_checked_at``,
NULLS FIRST) so the extension knows which ROLs to open detail modals for.
"""

import hashlib
from datetime import datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.court import Court
from app.models.ingest_key import IngestKey
from app.models.lawyer import Lawyer

PENDING_DETAIL_URL = "/api/v1/pjud/ingest/pending-detail"


@pytest.fixture
def ingest_key(db) -> str:
    plaintext = "test-ingest-key-plaintext"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    db.add(IngestKey(label="operator-test", key_hash=key_hash, is_active=True))
    db.commit()
    return plaintext


@pytest.fixture
def lawyer_with_cases(db):
    lawyer = Lawyer(rut="11111111-1", name="Test Lawyer", is_active=True)
    db.add(lawyer)
    db.flush()

    court = Court(code="T1-PENDING", name="Juzgado Pending Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    now = datetime.utcnow()
    # Never-checked case (NULL last_detail_checked_at) — must come first.
    never_checked = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1111-2026",
        competencia="civil",
        status="active",
        last_detail_checked_at=None,
    )
    # Older-checked case — second.
    older_checked = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-2222-2026",
        competencia="civil",
        status="active",
        last_detail_checked_at=now - timedelta(days=5),
    )
    # Recently-checked case — last.
    recently_checked = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-3333-2026",
        competencia="civil",
        status="active",
        last_detail_checked_at=now - timedelta(hours=1),
    )
    db.add_all([never_checked, older_checked, recently_checked])
    db.commit()

    # Approach C (unificar-modelo-causas, task 1b-5): pending-detail is now
    # scoped via case_lawyer_source, not Case.lawyer_id — seed a sighting row
    # for each case so this lawyer's pending-detail batch still includes them.
    now2 = datetime.utcnow()
    for c in (never_checked, older_checked, recently_checked):
        db.add(CaseLawyerSource(
            case_id=c.id, lawyer_id=lawyer.id, first_seen_at=now2, last_seen_at=now2,
        ))
    db.commit()

    return {"lawyer": lawyer, "cases": [never_checked, older_checked, recently_checked]}


class TestPendingDetailAuth:
    def test_missing_key_returns_401(self, client, lawyer_with_cases):
        response = client.get(PENDING_DETAIL_URL, params={"rut": "11111111-1"})
        assert response.status_code == 401

    def test_invalid_key_returns_403(self, client, ingest_key, lawyer_with_cases):
        response = client.get(
            PENDING_DETAIL_URL,
            params={"rut": "11111111-1"},
            headers={"X-Ingest-Key": "wrong-key"},
        )
        assert response.status_code == 403


class TestPendingDetailOrdering:
    def test_returns_stalest_first_nulls_first(self, client, ingest_key, lawyer_with_cases):
        response = client.get(
            PENDING_DETAIL_URL,
            params={"rut": "11111111-1", "competencia": "civil"},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        rols = [c["rol"] for c in data["cases"]]
        assert rols == ["C-1111-2026", "C-2222-2026", "C-3333-2026"]
        # Each entry carries an identifier + rol the extension needs.
        assert all("id" in c for c in data["cases"])

    def test_respects_limit(self, client, ingest_key, lawyer_with_cases):
        response = client.get(
            PENDING_DETAIL_URL,
            params={"rut": "11111111-1", "limit": 1},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["cases"]) == 1
        assert data["cases"][0]["rol"] == "C-1111-2026"

    def test_unknown_lawyer_returns_empty_list(self, client, ingest_key):
        response = client.get(
            PENDING_DETAIL_URL,
            params={"rut": "99999999-9"},
            headers={"X-Ingest-Key": ingest_key},
        )
        assert response.status_code == 200
        assert response.json()["cases"] == []
