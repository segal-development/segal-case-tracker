"""Sync cycle order: oldest successful sync first, never-synced first of all.

Found on 2026-09-14: the cycle iterated active lawyers in Postgres heap order
(no ORDER BY). With ~1h per lawyer and a worker that restarts daily, the same
6–7 lawyers synced every day while the rest — Carla among them — never got a
turn for two weeks. Ordering by staleness makes the rotation fair regardless of
where a cycle is interrupted.
"""

from datetime import datetime, timedelta

from app.config import settings
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.workers.sync_scheduler import _active_lawyer_ids_by_staleness


def _lawyer(db, rut, *, active=True):
    l = Lawyer(rut=rut, name=f"L {rut}", role="lawyer", is_active=active)
    db.add(l)
    db.commit()
    db.refresh(l)
    return l


def _sync(db, lawyer, *, ago_hours, status="completed"):
    at = datetime.utcnow() - timedelta(hours=ago_hours)
    db.add(SyncHistory(lawyer_id=lawyer.id, competencia="civil", started_at=at - timedelta(minutes=30),
                       completed_at=at if status == "completed" else None, status=status))
    db.commit()


class TestActiveLawyerIdsByStaleness:
    def test_never_synced_first_then_oldest_completed(self, db):
        recent = _lawyer(db, "10000000-1")
        stale = _lawyer(db, "20000000-2")
        never = _lawyer(db, "30000000-3")
        _sync(db, recent, ago_hours=1)
        _sync(db, stale, ago_hours=72)
        assert _active_lawyer_ids_by_staleness(db) == [never.id, stale.id, recent.id]

    def test_failed_attempts_do_not_count_as_a_sync(self, db):
        """Only COMPLETED syncs count: a lawyer whose recent attempts all failed is
        as stale as their last success (or never-synced)."""
        only_failed = _lawyer(db, "10000000-1")
        old_ok_recent_fail = _lawyer(db, "20000000-2")
        fresh = _lawyer(db, "30000000-3")
        _sync(db, only_failed, ago_hours=1, status="failed")
        _sync(db, old_ok_recent_fail, ago_hours=96)
        _sync(db, old_ok_recent_fail, ago_hours=2, status="failed")
        _sync(db, fresh, ago_hours=1)
        assert _active_lawyer_ids_by_staleness(db) == [only_failed.id, old_ok_recent_fail.id, fresh.id]

    def test_inactive_lawyers_are_excluded_and_ties_break_by_id(self, db):
        a = _lawyer(db, "10000000-1")
        b = _lawyer(db, "20000000-2")
        gone = _lawyer(db, "30000000-3", active=False)
        assert _active_lawyer_ids_by_staleness(db) == [a.id, b.id]
        assert gone.id not in _active_lawyer_ids_by_staleness(db)


class TestSyncLawyerIdsSetting:
    """SYNC_LAWYER_IDS narrows the cycle (split load across machines, isolate one
    lawyer in a test). Empty must leave production behaviour untouched."""

    def test_empty_setting_visits_every_active_lawyer(self, db, monkeypatch):
        a = _lawyer(db, "10000000-1")
        b = _lawyer(db, "20000000-2")
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", "")
        assert _active_lawyer_ids_by_staleness(db) == [a.id, b.id]

    def test_only_listed_lawyers_are_visited(self, db, monkeypatch):
        a = _lawyer(db, "10000000-1")
        _lawyer(db, "20000000-2")
        c = _lawyer(db, "30000000-3")
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", f"{a.id},{c.id}")
        assert _active_lawyer_ids_by_staleness(db) == [a.id, c.id]

    def test_staleness_order_wins_over_the_order_in_the_setting(self, db, monkeypatch):
        recent = _lawyer(db, "10000000-1")
        stale = _lawyer(db, "20000000-2")
        never = _lawyer(db, "30000000-3")
        _sync(db, recent, ago_hours=1)
        _sync(db, stale, ago_hours=72)
        # Listed freshest-first on purpose: the setting must not dictate order.
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", f"{recent.id},{stale.id},{never.id}")
        assert _active_lawyer_ids_by_staleness(db) == [never.id, stale.id, recent.id]

    def test_unknown_id_is_ignored(self, db, monkeypatch):
        a = _lawyer(db, "10000000-1")
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", f"{a.id},999999")
        assert _active_lawyer_ids_by_staleness(db) == [a.id]

    def test_inactive_lawyer_stays_excluded_even_if_listed(self, db, monkeypatch):
        a = _lawyer(db, "10000000-1")
        gone = _lawyer(db, "20000000-2", active=False)
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", f"{a.id},{gone.id}")
        assert _active_lawyer_ids_by_staleness(db) == [a.id]

    def test_unparsable_setting_visits_nobody_instead_of_everybody(self, db, monkeypatch):
        """A typo while isolating one lawyer must not silently widen to the whole
        firm."""
        _lawyer(db, "10000000-1")
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", "abc")
        assert _active_lawyer_ids_by_staleness(db) == []

    def test_property_skips_non_numeric_tokens_and_whitespace(self, monkeypatch):
        monkeypatch.setattr(settings, "SYNC_LAWYER_IDS", " 1, 7 ,x,,16 ")
        assert settings.sync_lawyer_ids_list == [1, 7, 16]
