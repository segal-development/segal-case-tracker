"""SyncService.last_sync_age_hours feeds the 'why was this lawyer skipped' line."""
from datetime import datetime, timedelta

import pytest

from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.services.sync_service import SyncService


def _lawyer(db) -> Lawyer:
    lawyer = Lawyer(rut="60000001-1", name="Age", is_active=True)
    db.add(lawyer)
    db.commit()
    return lawyer


def test_age_is_none_without_a_successful_sync(db):
    lawyer = _lawyer(db)
    assert SyncService(db).last_sync_age_hours(lawyer.id, "civil") is None


def test_age_is_hours_since_last_successful_sync(db):
    lawyer = _lawyer(db)
    rec = SyncHistory(lawyer_id=lawyer.id, competencia="civil",
                      started_at=datetime.utcnow() - timedelta(hours=2, minutes=30),
                      triggered_by="scheduled")
    rec.cases_found = 0
    rec.status = "completed"
    rec.completed_at = datetime.utcnow() - timedelta(hours=1, minutes=30)
    db.add(rec)
    db.commit()

    age = SyncService(db).last_sync_age_hours(lawyer.id, "civil")

    assert age == pytest.approx(1.5, abs=0.02)
