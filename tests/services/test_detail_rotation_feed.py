"""Delta-sync T2: the estado-diario feed gets priority in the detail rotation,
with a cap (feed slots per batch) and a floor (slots the age rotation keeps).

Measured 2026-10-15: lawyer 17 got 190 notified causas in one day. Without the
cap they would eat six whole batches and freeze that lawyer's forgotten queue.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.sync_service import _select_cases_for_detail_rotation

SINCE = datetime(2026, 10, 16, 0, 0, 0)
LONG_AGO = datetime(2026, 1, 1)


def _lawyer_and_court(db):
    lawyer = Lawyer(rut="12345678-5", name="Lawyer", is_active=True)
    court = db.query(Court).first() or Court(
        code="T1", name="Tribunal", region="RM", type="civil"
    )
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _seed(db, lawyer, court, rol, checked=None, **kw):
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", last_detail_checked_at=checked, **kw,
    )
    db.add(case)
    db.flush()
    return case


def _api(rol, token="tok"):
    m = MagicMock()
    m.rol = rol
    m.case_token = token
    return m


def _world(db, *, feed_n, other_n):
    """feed_n causas in the feed + other_n causas outside it.

    Non-feed causas are the OLDEST (checked long ago, staggered), so the age
    rotation would pick them first if it had room. Feed causas were checked
    after them, so on pure age order they would come last.
    """
    lawyer, court = _lawyer_and_court(db)
    base = datetime(2026, 5, 1)
    others = [
        _seed(db, lawyer, court, f"C-{i}-2022", base + timedelta(minutes=i))
        for i in range(other_n)
    ]
    feed = [
        _seed(db, lawyer, court, f"C-{i}-2025", datetime(2026, 8, 1) + timedelta(minutes=i))
        for i in range(feed_n)
    ]
    db.commit()
    api = [_api(c.rol) for c in others + feed]
    return lawyer, [c.rol for c in others], [c.rol for c in feed], api


@pytest.fixture
def feed_on(monkeypatch):
    monkeypatch.setattr(settings, "DETAIL_FEED_PRIORITY_ENABLED", True)
    monkeypatch.setattr(settings, "DETAIL_FEED_MAX_PER_BATCH", 20)
    monkeypatch.setattr(settings, "DETAIL_MIN_YEAR", 2021)


def _select(db, lawyer, api, feed_rols, batch=30, **kw):
    return [
        a.rol
        for a in _select_cases_for_detail_rotation(
            db, lawyer.id, "civil", api, batch_size=batch,
            priority_rols=feed_rols, priority_since=SINCE, **kw,
        )
    ]


class TestDefaults:
    def test_flag_is_off_and_cap_is_twenty(self):
        from app.config import Settings

        assert Settings.model_fields["DETAIL_FEED_PRIORITY_ENABLED"].default is False
        assert Settings.model_fields["DETAIL_FEED_MAX_PER_BATCH"].default == 20


class TestFlagOff:
    def test_selection_is_identical_with_and_without_the_feed(self, db, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_FEED_PRIORITY_ENABLED", False)
        lawyer, others, feed, api = _world(db, feed_n=50, other_n=60)
        without = _select(db, lawyer, api, None)
        with_feed = _select(db, lawyer, api, set(feed))
        assert with_feed == without
        assert with_feed[:30] == others[:30]


class TestFeedPriority:
    def test_feed_causas_come_first_and_the_rest_is_age_rotation(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=5, other_n=60)
        got = _select(db, lawyer, api, set(feed))
        assert got[:5] == feed
        assert got[5:] == others[:25]
        assert len(got) == 30

    def test_cap_is_respected(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        got = _select(db, lawyer, api, set(feed))
        assert len([r for r in got if r in set(feed)]) == 20

    def test_floor_is_always_respected_even_with_190_notified(self, db, feed_on):
        """The most important test of the work."""
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        got = _select(db, lawyer, api, set(feed))
        rotation_part = [r for r in got if r not in set(feed)]
        assert len(got) == 30
        assert rotation_part == others[:10], "age rotation must keep its 10 slots"

    def test_cap_is_configurable(self, db, feed_on, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_FEED_MAX_PER_BATCH", 5)
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        got = _select(db, lawyer, api, set(feed))
        assert len([r for r in got if r in set(feed)]) == 5
        assert [r for r in got if r not in set(feed)] == others[:25]

    def test_cap_zero_means_no_feed_slots(self, db, feed_on, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_FEED_MAX_PER_BATCH", 0)
        lawyer, others, feed, api = _world(db, feed_n=10, other_n=60)
        assert _select(db, lawyer, api, set(feed)) == others[:30]

    def test_feed_hits_beyond_the_cap_wait_their_turn_not_the_rotation_slots(
        self, db, feed_on
    ):
        """Hits over the cap must not leak into the age-rotation slots."""
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=3)
        got = _select(db, lawyer, api, set(feed))
        assert len([r for r in got if r in set(feed)]) == 20
        assert len(got) == 23  # 20 feed + the only 3 non-feed causas

    def test_empty_feed_behaves_like_today(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=5, other_n=60)
        baseline = _select(db, lawyer, api, None)
        assert _select(db, lawyer, api, set()) == baseline
        assert _select(db, lawyer, api, []) == baseline

    def test_feed_rol_missing_from_the_db_is_ignored(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=3, other_n=60)
        got = _select(db, lawyer, api, set(feed) | {"C-999-2026"})
        assert "C-999-2026" not in got
        assert got[:3] == feed

    def test_feed_rol_without_a_live_token_is_ignored(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=3, other_n=60)
        api = [a for a in api if a.rol != feed[0]] + [_api(feed[0], token=None)]
        got = _select(db, lawyer, api, set(feed))
        assert feed[0] not in got
        assert got[:2] == feed[1:]

    def test_feed_rols_are_matched_normalised(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=2, other_n=60)
        got = _select(db, lawyer, api, {f" {feed[0].lower()} "})
        assert got[0] == feed[0]

    def test_a_causa_already_checked_since_the_feed_date_is_not_prioritised(
        self, db, feed_on
    ):
        """Otherwise the same 20 hits would sit in the feed slots every cycle
        for the rest of the day."""
        lawyer, others, feed, api = _world(db, feed_n=3, other_n=60)
        done = db.query(Case).filter(Case.rol == feed[0]).one()
        done.last_detail_checked_at = SINCE + timedelta(hours=2)
        db.commit()
        got = _select(db, lawyer, api, set(feed))
        assert got[:2] == feed[1:]
        # it is an ordinary causa again: it may only come back by age
        assert feed[0] not in got

    def test_podada_feed_causa_stays_excluded(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=3, other_n=60)
        case = db.query(Case).filter(Case.rol == feed[0]).one()
        case.poda_at = datetime(2026, 9, 1)
        db.commit()
        got = _select(db, lawyer, api, set(feed))
        assert feed[0] not in got

    def test_year_scope_still_applies_to_feed_causas(self, db, feed_on):
        lawyer, court = _lawyer_and_court(db)
        old = _seed(db, lawyer, court, "C-1-2019", LONG_AGO)
        new = _seed(db, lawyer, court, "C-1-2025", LONG_AGO)
        db.commit()
        api = [_api(old.rol), _api(new.rol)]
        assert _select(db, lawyer, api, {old.rol, new.rol}) == [new.rol]

    def test_batch_smaller_than_the_cap_never_exceeds_the_batch(self, db, feed_on):
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        assert len(_select(db, lawyer, api, set(feed), batch=8)) == 8


class TestCapScalesWithTheBatch:
    """The feed cap is a SHARE of the batch, not an absolute number.

    The cap exists to keep a floor (``batch_size - cap``) for the age
    rotation: on 2026-10-15 lawyer 17 received 190 feed notifications in a day
    and without the floor they would freeze its never-reviewed queue for days.

    A caller passing a batch smaller than the cap (CONSULTA_BATCH = 15,
    cartera gets a batch SMALLER than the cap, and an absolute cap would eat
    all of it -- the age-rotation floor would be ZERO exactly for the lawyers
    the floor was written to protect. Scaling the cap with the batch keeps the
    ratio the firm measured, whatever the batch size is.
    """

    def test_a_batch_smaller_than_the_cap_still_keeps_its_rotation_floor(
        self, db, feed_on, monkeypatch
    ):
        # Base 30 / cap 20 -> a batch of 15 gets 10 feed slots and 5 age slots.
        monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 30)
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        got = _select(db, lawyer, api, set(feed), batch=15)
        assert len(got) == 15
        assert len([r for r in got if r in set(feed)]) == 10
        assert [r for r in got if r not in set(feed)] == others[:5]

    def test_a_batch_bigger_than_the_base_scales_the_cap_up(
        self, db, feed_on, monkeypatch
    ):
        monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 30)
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        got = _select(db, lawyer, api, set(feed), batch=60)
        assert len([r for r in got if r in set(feed)]) == 40
        assert [r for r in got if r not in set(feed)] == others[:20]

    def test_a_batch_equal_to_the_base_is_unchanged(self, db, feed_on, monkeypatch):
        monkeypatch.setattr(settings, "DETAIL_BATCH_SIZE", 30)
        lawyer, others, feed, api = _world(db, feed_n=190, other_n=60)
        got = _select(db, lawyer, api, set(feed), batch=30)
        assert len([r for r in got if r in set(feed)]) == 20
        assert [r for r in got if r not in set(feed)] == others[:10]
