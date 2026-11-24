"""PJUD request/rejection counters: counting, upsert flush, failure isolation, alert."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.models.pjud_request_count import PjudRequestCount
from app.scrapper.pjud.resilience import rate_limiter
from app.services import egress_guard, pjud_metrics as pm

T0 = datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc)
HOUR0 = datetime(2026, 11, 24, 15)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pm.reset_state()
    egress_guard.reset_state()
    monkeypatch.setattr(pm, "NOW", lambda: T0)
    monkeypatch.setattr(egress_guard, "_cache", (0.0, (True, "186.9.145.83", "allowed")))
    yield
    pm.reset_state()
    egress_guard.reset_state()


def _rows(db):
    return {
        (r.lawyer_id, r.egress_ip, r.action, r.kind): r.count
        for r in db.query(PjudRequestCount).all()
    }


def test_counts_per_action_account_and_ip(db):
    tok = pm.set_lawyer(7)
    pm.record_request("list")
    pm.record_request("detail")
    pm.record_request("detail")
    pm.reset_lawyer(tok)
    pm.record_request("login")
    pm.flush(db)
    assert _rows(db) == {
        (7, "186.9.145.83", "list", "request"): 1,
        (7, "186.9.145.83", "detail", "request"): 2,
        (None, "186.9.145.83", "login", "request"): 1,
    }
    assert db.query(PjudRequestCount).first().hour_utc == HOUR0


def test_unknown_ip_when_guard_has_no_cache(db, monkeypatch):
    monkeypatch.setattr(egress_guard, "_cache", None)
    pm.record_request("list")
    pm.flush(db)
    assert _rows(db) == {(None, "unknown", "list", "request"): 1}


def test_never_performs_an_ip_lookup(monkeypatch):
    boom = MagicMock(side_effect=AssertionError("lookup"))
    monkeypatch.setattr(egress_guard, "egress_allowed", boom)
    monkeypatch.setattr(egress_guard, "egress_allowed_cached", boom)
    monkeypatch.setattr(egress_guard, "current_egress_ip", boom)
    pm.record_request("list")
    boom.assert_not_called()


def test_flush_accumulates_across_flushes_and_never_double_counts(db):
    tok = pm.set_lawyer(3)
    pm.record_request("detail")
    pm.record_request("detail")
    pm.flush(db)
    pm.flush(db)  # nothing new: must not add again
    assert _rows(db) == {(3, "186.9.145.83", "detail", "request"): 2}
    pm.record_request("detail")
    pm.flush(db)
    assert _rows(db) == {(3, "186.9.145.83", "detail", "request"): 3}
    pm.reset_lawyer(tok)
    pm.record_request("detail")  # NULL account bucket accumulates too
    pm.flush(db)
    pm.record_request("detail")
    pm.flush(db)
    assert _rows(db)[(None, "186.9.145.83", "detail", "request")] == 2
    assert db.query(PjudRequestCount).count() == 2


def test_new_hour_is_a_new_bucket(db, monkeypatch):
    pm.record_request("list")
    monkeypatch.setattr(pm, "NOW", lambda: T0 + timedelta(hours=1))
    pm.record_request("list")
    pm.flush(db)
    assert sorted(r.hour_utc for r in db.query(PjudRequestCount)) == [
        HOUR0, HOUR0 + timedelta(hours=1)]


def test_flush_failure_does_not_raise_keeps_counts_and_logs_once(db, caplog):
    pm.record_request("list")
    bad = MagicMock()
    bad.get_bind.side_effect = RuntimeError("db down")
    pm.flush(bad)
    pm.flush(bad)
    assert sum("PJUD metrics" in r.message for r in caplog.records) == 1
    pm.flush(db)  # the counts survived the failures
    assert _rows(db) == {(None, "186.9.145.83", "list", "request"): 1}


def test_counts_recorded_during_flush_are_kept(db, monkeypatch):
    pm.record_request("list")
    orig = pm._upsert

    def upsert_then_record(d, snap):
        orig(d, snap)
        pm.record_request("list")  # arrives mid-flush

    monkeypatch.setattr(pm, "_upsert", upsert_then_record)
    pm.flush(db)
    monkeypatch.setattr(pm, "_upsert", orig)
    pm.flush(db)
    assert _rows(db) == {(None, "186.9.145.83", "list", "request"): 2}


def test_rejection_counted_and_one_alert_per_hour_per_ip_and_kind(db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.telegram_notifier.send_telegram", lambda t: sent.append(t) or True)
    tok = pm.set_lawyer(5)
    pm.record_rejection(pm.KIND_SHAPE, "detail")
    pm.record_rejection(pm.KIND_SHAPE, "detail")
    pm.flush(db)
    assert _rows(db) == {(5, "186.9.145.83", "detail", "shape_challenge"): 2}
    assert len(sent) == 1
    assert "shape_challenge" in sent[0] and "186.9.145.83" in sent[0] and "x2" in sent[0] and "5" in sent[0]

    pm.record_rejection(pm.KIND_SHAPE, "detail")
    pm.flush(db)
    assert len(sent) == 1  # same IP+kind within the hour
    pm.record_rejection(pm.KIND_HTTP_429, "list")
    pm.flush(db)
    assert len(sent) == 2  # other kind
    monkeypatch.setattr(pm, "NOW", lambda: T0 + timedelta(minutes=61))
    pm.record_rejection(pm.KIND_SHAPE, "detail")
    pm.flush(db)
    assert len(sent) == 3  # an hour later
    pm.reset_lawyer(tok)


def test_requests_alone_never_alert(db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.telegram_notifier.send_telegram", lambda t: sent.append(t))
    pm.record_request("list")
    pm.flush(db)
    assert sent == []


def test_alert_failure_does_not_raise(db, monkeypatch):
    def boom(_):
        raise RuntimeError("telegram")
    monkeypatch.setattr("app.services.telegram_notifier.send_telegram", boom)
    pm.record_rejection(pm.KIND_HTTP_403, "list")
    pm.flush(db)
    assert _rows(db) == {(None, "186.9.145.83", "list", "http_403"): 1}


def test_http_status_only_counts_403_and_429():
    for s in (200, 404, 500, "x", None):
        pm.record_http_status(s, "list")
    assert pm._counts == {}
    pm.record_http_status("403", "list")
    pm.record_http_status(429, "document")
    assert sorted(k[4] for k in pm._counts) == ["http_403", "http_429"]


# --- choke point: the action limiter -------------------------------------

def test_limiter_acquire_records_request_per_action():
    rate_limiter._rate_limiters.clear()
    tok = pm.set_lawyer(9)
    asyncio.run(rate_limiter.pjud_action_limiter("list").acquire())
    asyncio.run(rate_limiter.pjud_action_limiter("estado_diario").acquire())
    pm.reset_lawyer(tok)
    assert {(k[1], k[3], k[4]): v for k, v in pm._counts.items()} == {
        (9, "list", "request"): 1, (9, "estado_diario", "request"): 1}
    rate_limiter._rate_limiters.clear()


def test_non_pjud_limiter_is_not_counted():
    rate_limiter._rate_limiters.clear()
    asyncio.run(rate_limiter.get_rate_limiter("other").acquire())
    assert pm._counts == {}
    rate_limiter._rate_limiters.clear()


def test_limiter_behaviour_unchanged_when_metrics_break(monkeypatch):
    rate_limiter._rate_limiters.clear()
    monkeypatch.setattr(pm, "record_request", MagicMock(side_effect=RuntimeError("x")))
    assert asyncio.run(rate_limiter.pjud_action_limiter("detail").acquire()) is True
    rate_limiter._rate_limiters.clear()


def test_rejection_recorded_between_snapshot_and_subtraction_survives(db, monkeypatch):
    pm.record_request("list")
    orig = pm._upsert

    def upsert_then_reject(d, snap):
        orig(d, snap)
        pm.record_rejection(pm.KIND_HTTP_429, "list")  # arrives after the snapshot

    monkeypatch.setattr(pm, "_upsert", upsert_then_reject)
    monkeypatch.setattr("app.services.telegram_notifier.send_telegram", lambda t: True)
    pm.flush(db)
    assert _rows(db) == {(None, "186.9.145.83", "list", "request"): 1}
    assert list(pm._counts.values()) == [1] and list(pm._counts)[0][4] == "http_429"
    monkeypatch.setattr(pm, "_upsert", orig)
    pm.flush(db)
    assert _rows(db)[(None, "186.9.145.83", "list", "http_429")] == 1


def test_http_status_overflow_never_raises():
    pm.record_http_status(float("inf"), "list")
    pm.record_http_status(10**400, "list")
    assert pm._counts == {}
