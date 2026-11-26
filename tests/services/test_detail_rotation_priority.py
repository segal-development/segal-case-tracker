"""S5 (causas prioritarias): a causa a lawyer marked as priority is read more
often by the station.

Order: requested documents > fatal plazo <= DETAIL_FATAL_PLAZO_DAYS > pending
priority causa > the existing rotation. The mark never overrides ``poda_at`` or
``DETAIL_MIN_YEAR``.
"""

from datetime import date, datetime, timedelta

import pytest

from app.config import Settings, settings
from app.models.case_deadline import CaseDeadline
from app.models.case_priority import CasePriority
from app.models.document import Document
from app.services.sync_service import _select_cases_for_detail_rotation

from tests.services.test_detail_rotation import (  # reuse the file's helpers
    _make_api_case,
    _seed_case,
)

NOW = datetime.utcnow()


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "DETAIL_PRIORITY_ENABLED", True)
    monkeypatch.setattr(settings, "DETAIL_PRIORITY_MIN_HOURS", 4)
    monkeypatch.setattr(settings, "DETAIL_PRIORITY_MAX_PER_BATCH", 10)
    monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 30)
    monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2021)
    monkeypatch.setattr(settings, "DETAIL_FATAL_PLAZO_DAYS", 2)


def _mark(db, case, lawyer_id=1):
    db.add(CasePriority(case_id=case.id, lawyer_id=lawyer_id))
    db.flush()


def _select(db, rols, batch=30):
    api = [_make_api_case(r) for r in rols]
    got = _select_cases_for_detail_rotation(db, 1, "civil", api, batch_size=batch)
    return [a.rol for a in got]


def _old(db, rol, days=30, **kw):
    return _seed_case(
        db, 1, "civil", rol, last_detail_checked_at=NOW - timedelta(days=days), **kw
    )


class TestDefaults:
    def test_settings_defaults(self):
        f = Settings.model_fields
        assert f["DETAIL_PRIORITY_ENABLED"].default is True
        assert f["DETAIL_PRIORITY_MIN_HOURS"].default == 4
        assert f["DETAIL_PRIORITY_MAX_PER_BATCH"].default == 10


class TestPriorityOrdering:
    def test_stale_priority_case_jumps_ahead_of_an_older_non_priority_one(self, db):
        _old(db, "C-1-2025", days=60)  # older, not marked
        marked = _old(db, "C-2-2025", days=1)  # newer but marked and > 4h
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-2-2025", "C-1-2025"]

    def test_never_read_priority_case_is_boosted(self, db):
        _old(db, "C-1-2025", days=60)
        marked = _seed_case(db, 1, "civil", "C-2-2025", last_detail_checked_at=None)
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-2-2025", "C-1-2025"]

    def test_mark_from_any_lawyer_counts(self, db):
        _old(db, "C-1-2025", days=60)
        marked = _old(db, "C-2-2025", days=1)
        _seed_case(db, 2, "civil", "C-X-2025")  # creates lawyer 2
        _mark(db, marked, lawyer_id=2)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-2-2025", "C-1-2025"]

    def test_freshly_read_priority_case_is_not_boosted(self, db):
        _old(db, "C-1-2025", days=60)
        marked = _seed_case(
            db, 1, "civil", "C-2-2025",
            last_detail_checked_at=NOW - timedelta(hours=1),
        )
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-1-2025", "C-2-2025"]

    def test_min_hours_is_configurable(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_PRIORITY_MIN_HOURS", 0)
        _old(db, "C-1-2025", days=60)
        marked = _seed_case(
            db, 1, "civil", "C-2-2025",
            last_detail_checked_at=NOW - timedelta(minutes=5),
        )
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-2-2025", "C-1-2025"]

    def test_fatal_plazo_still_beats_priority(self, db):
        fatal = _seed_case(
            db, 1, "civil", "C-F-2026", last_detail_checked_at=NOW - timedelta(minutes=1)
        )
        db.add(
            CaseDeadline(
                case_id=fatal.id, deadline_type="acreditar_poder_3d",
                legal_basis="test", due_date=date.today() + timedelta(days=1),
                triggered_at=date.today() - timedelta(days=2), status="active",
            )
        )
        marked = _old(db, "C-P-2025", days=20)
        _mark(db, marked)
        _old(db, "C-R-2025", days=90)
        db.commit()
        got = _select(db, ["C-R-2025", "C-P-2025", "C-F-2026"])
        assert got == ["C-F-2026", "C-P-2025", "C-R-2025"]

    def test_requested_document_still_beats_fatal_and_priority(self, db):
        req = _seed_case(
            db, 1, "civil", "C-D-2025", last_detail_checked_at=NOW - timedelta(minutes=1)
        )
        db.add(
            Document(
                case_id=req.id, doc_type="resolution", status="pending",
                prioridad_solicitada_at=NOW - timedelta(minutes=5),
            )
        )
        fatal = _seed_case(
            db, 1, "civil", "C-F-2026", last_detail_checked_at=NOW - timedelta(minutes=1)
        )
        db.add(
            CaseDeadline(
                case_id=fatal.id, deadline_type="acreditar_poder_3d",
                legal_basis="test", due_date=date.today() + timedelta(days=1),
                triggered_at=date.today() - timedelta(days=2), status="active",
            )
        )
        marked = _old(db, "C-P-2025", days=20)
        _mark(db, marked)
        _old(db, "C-R-2025", days=90)
        db.commit()
        got = _select(db, ["C-R-2025", "C-P-2025", "C-F-2026", "C-D-2025"])
        assert got == ["C-D-2025", "C-F-2026", "C-P-2025", "C-R-2025"]


class TestCap:
    def _world(self, db, marked_n, other_n):
        others = [_old(db, f"C-{i}-2022", days=100 - i) for i in range(other_n)]
        marked = [_old(db, f"C-{i}-2025", days=10 - i % 5) for i in range(marked_n)]
        for c in marked:
            _mark(db, c)
        db.commit()
        return [c.rol for c in others], {c.rol for c in marked}

    def test_cap_is_respected_and_the_rest_goes_to_rotation(self, db):
        others, marked = self._world(db, marked_n=25, other_n=40)
        got = _select(db, others + sorted(marked))
        assert len(got) == 30
        assert len([r for r in got if r in marked]) == 10
        assert [r for r in got if r not in marked] == others[:20]

    def test_leftover_slots_go_to_rotation(self, db):
        others, marked = self._world(db, marked_n=3, other_n=40)
        got = _select(db, others + sorted(marked))
        assert len(got) == 30
        assert set(got[:3]) == marked
        assert got[3:] == others[:27]

    def test_hits_beyond_the_cap_do_not_leak_into_rotation_slots(self, db):
        others, marked = self._world(db, marked_n=25, other_n=3)
        got = _select(db, others + sorted(marked))
        assert len(got) == 13  # 10 priority + the only 3 non-marked

    def test_cap_scales_with_a_smaller_batch(self, db):
        others, marked = self._world(db, marked_n=25, other_n=40)
        got = _select(db, others + sorted(marked), batch=15)
        assert len(got) == 15
        assert len([r for r in got if r in marked]) == 5  # round(10 * 15 / 30)

    def test_cap_zero_means_no_priority_slots(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_PRIORITY_MAX_PER_BATCH", 0)
        others, marked = self._world(db, marked_n=5, other_n=40)
        got = _select(db, others + sorted(marked))
        assert got == others[:30]

    def test_composes_with_the_feed_cap(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_FEED_PRIORITY_ENABLED", True)
        monkeypatch.setattr(settings, "DETAIL_FEED_MAX_PER_BATCH", 20)
        others, marked = self._world(db, marked_n=25, other_n=40)
        feed = set(others[30:])
        api = [_make_api_case(r) for r in others + sorted(marked)]
        got = [
            a.rol
            for a in _select_cases_for_detail_rotation(
                db, 1, "civil", api, batch_size=30,
                priority_rols=feed, priority_since=NOW + timedelta(days=1),
            )
        ]
        assert len(got) == 30
        assert len([r for r in got if r in feed]) == 10  # only 10 exist
        assert len([r for r in got if r in marked]) == 10
        assert len([r for r in got if r not in feed and r not in marked]) == 10


class TestStillExcluded:
    def test_podada_priority_case_is_excluded(self, db):
        marked = _old(db, "C-1-2025")
        marked.poda_at = NOW
        _old(db, "C-2-2025", days=60)
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-2-2025"]

    def test_pre_min_year_priority_case_is_excluded(self, db):
        marked = _old(db, "C-1-2019")
        _old(db, "C-2-2025", days=60)
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2019", "C-2-2025"]) == ["C-2-2025"]


class TestFlagOff:
    def test_flag_off_keeps_the_previous_order(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_PRIORITY_ENABLED", False)
        _old(db, "C-1-2025", days=60)
        marked = _old(db, "C-2-2025", days=1)
        _mark(db, marked)
        db.commit()
        assert _select(db, ["C-1-2025", "C-2-2025"]) == ["C-1-2025", "C-2-2025"]
