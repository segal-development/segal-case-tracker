"""Tests for GET /api/v1/sync/status — the sidebar "Última conexión PJUD" indicator.

Regression for the "sin datos" bug: admin/auditor have no caseload of their own,
so scoping the indicator to their (empty) cartera returned null even while the
firm was actively syncing. The endpoint now reports FIRM-WIDE activity for
transversal roles, while regular lawyers stay scoped to their own caseload.
"""
from datetime import datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court
from app.models.sync_history import SyncHistory

STATUS_URL = "/api/v1/sync/status?competencia=civil"


def _h(rut):
    return {"Authorization": "Bearer " + create_access_token({"sub": rut})}


@pytest.fixture
def court(db):
    c = Court(code="C-SYNC-SB", name="Test Court", region="RM", type="civil")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_admin_sees_firm_wide_activity(client, db, court):
    """Admin has 0 cases of their own, but the firm is actively syncing → the
    indicator must report the firm-wide activity, NOT 'sin datos'."""
    admin = Lawyer(rut="16021492-9", name="Carla Admin", role="admin")
    worker = Lawyer(rut="66666666-6", name="Worker", role="lawyer")
    db.add_all([admin, worker])
    db.commit()
    db.refresh(admin)
    db.refresh(worker)

    activity = datetime.utcnow() - timedelta(minutes=10)
    db.add(Case(lawyer_id=worker.id, court_id=court.id, rol="W-1-2026",
                competencia="civil", last_detail_checked_at=activity))
    db.add(SyncHistory(lawyer_id=worker.id, competencia="civil", status="completed",
                       started_at=activity, completed_at=activity))
    db.commit()

    r = client.get(STATUS_URL, headers=_h("16021492-9"))
    assert r.status_code == 200
    body = r.json()
    assert body["last_activity"] is not None   # antes: None → "sin datos"
    assert body["last_sync"] is not None       # sync firm-wide
    assert body["needs_sync"] is False         # sincronizó hace 10 min (< 4h)


def test_regular_lawyer_scoped_to_own_caseload(client, db, court):
    """A regular lawyer still sees ONLY their own caseload activity — not the
    firm's more recent syncs from other lawyers."""
    a = Lawyer(rut="77777777-7", name="Lawyer A", role="lawyer")
    b = Lawyer(rut="88888888-8", name="Lawyer B", role="lawyer")
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)

    a_time = datetime.utcnow() - timedelta(days=2)
    b_time = datetime.utcnow() - timedelta(minutes=5)
    db.add(Case(lawyer_id=a.id, court_id=court.id, rol="A-1-2026",
                competencia="civil", last_detail_checked_at=a_time))
    db.add(Case(lawyer_id=b.id, court_id=court.id, rol="B-1-2026",
                competencia="civil", last_detail_checked_at=b_time))
    db.commit()

    r = client.get(STATUS_URL, headers=_h("77777777-7"))
    assert r.status_code == 200
    body = r.json()
    assert body["last_activity"] is not None
    # A ve su propia causa (2 días atrás), NO la de B (5 min atrás).
    la = datetime.fromisoformat(body["last_activity"]).replace(tzinfo=None)
    assert (datetime.utcnow() - la) > timedelta(hours=1)
