"""Egress guard allow-list (S1-S4). No real network, no real DB."""
import pytest

from app.config import settings
from app.models.app_text_setting import AppTextSetting  # noqa: F401
from app.services import app_settings
from app.services import egress_guard as eg
from tests.conftest import TestingSessionLocal

OFFICE = "186.10.251.189"
FIVE_G = "186.9.145.83"
OTHER = "181.42.38.231"


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _iso(monkeypatch, db):
    eg.reset_state()
    monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", "")
    monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", "", raising=False)
    clock = Clock()
    clock.t = 1000.0
    monkeypatch.setattr(eg, "EGRESS_CLOCK", clock)
    sent = []
    monkeypatch.setattr(eg, "send_telegram", lambda t: sent.append(t) or True)
    monkeypatch.setattr(eg, "_session_factory", TestingSessionLocal, raising=False)
    monkeypatch.setattr(eg, "_default_fetchers", lambda: [_boom])
    yield clock, sent
    eg.reset_state()


def _boom(timeout):
    raise AssertionError("real network lookup attempted")


def _use(monkeypatch, value):
    monkeypatch.setattr(eg, "_default_fetchers", lambda: [lambda timeout: value])


def _db_list(db, raw):
    app_settings.set_text(db, app_settings.KEY_IPS_PERMITIDAS, raw)
    db.commit()


class TestParse:
    def test_valid_entries(self):
        nets = eg.parse_allowed_networks(" 1.2.3.4 , 10.0.0.0/8,,2001:db8::/32 ")
        assert [str(n) for n in nets] == ["1.2.3.4/32", "10.0.0.0/8", "2001:db8::/32"]

    def test_invalid_entries_are_skipped(self):
        assert [str(n) for n in eg.parse_allowed_networks("nope, 1.2.3.4")] == ["1.2.3.4/32"]


class TestAllowList:
    def test_exact_ip_match(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, FIVE_G)
        assert eg.egress_allowed() == (True, FIVE_G, "allowed")

    def test_cidr_match_v4_and_v6(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", "186.9.145.0/24, 2001:db8::/32")
        _use(monkeypatch, "186.9.145.200")
        assert eg.egress_allowed()[0] is True
        _use(monkeypatch, "2001:db8::5")
        assert eg.egress_allowed()[0] is True

    def test_non_matching_ip_refused_with_alert(self, monkeypatch, _iso):
        _, sent = _iso
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed() == (False, OTHER, "not_allowed")
        assert sent == [
            f"El worker se detuvo: la IP {OTHER} no está en la lista de redes permitidas."
        ]

    def test_recovery_alert_when_back_in_list(self, monkeypatch, _iso):
        _, sent = _iso
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, OTHER)
        eg.egress_allowed()
        _use(monkeypatch, FIVE_G)
        eg.egress_allowed()
        assert sent[1:] == [f"El worker volvió a una red permitida (IP {FIVE_G})."]

    def test_unknown_ip_refused(self, monkeypatch, _iso):
        _, sent = _iso
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, "garbage")
        assert eg.egress_allowed() == (False, None, "unknown")
        assert sent == [
            "No se pudo verificar la IP de salida; el worker espera antes de consultar el PJUD."
        ]

    def test_deny_list_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", OFFICE)
        monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", OFFICE)
        _use(monkeypatch, OFFICE)
        assert eg.egress_allowed() == (False, OFFICE, "forbidden")

    def test_both_empty_is_off_without_lookup(self):
        assert eg.egress_allowed() == (True, None, "off")
        assert eg.egress_allowed_cached() == (True, None, "off")

    def test_only_deny_list_behaves_as_before(self, monkeypatch):
        monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", OFFICE)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed() == (True, OTHER, "allowed")

    def test_all_invalid_configured_entries_fail_closed(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", "typo")
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed() == (False, OTHER, "not_allowed")


class TestDbSource:
    def test_db_takes_precedence_over_env(self, monkeypatch, db):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", OTHER)
        _db_list(db, FIVE_G)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed()[:3:2] == (False, "not_allowed")
        _use(monkeypatch, FIVE_G)
        assert eg.egress_allowed()[0] is True

    def test_empty_db_value_turns_allow_list_off(self, monkeypatch, db):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _db_list(db, "")
        assert eg.egress_allowed() == (True, None, "off")

    def test_absent_db_row_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed()[0] is False

    def test_db_error_falls_back_to_env_and_never_allows_outside_it(self, monkeypatch):
        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(eg, "_session_factory", broken)
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed() == (False, OTHER, "not_allowed")
        _use(monkeypatch, FIVE_G)
        assert eg.egress_allowed()[0] is True

    def test_db_error_with_empty_env_is_off(self, monkeypatch):
        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(eg, "_session_factory", broken)
        assert eg.egress_allowed() == (True, None, "off")

    def test_changes_seen_after_cache_window(self, monkeypatch, db, _iso):
        clock, _ = _iso
        _db_list(db, FIVE_G)
        _use(monkeypatch, FIVE_G)
        assert eg.egress_allowed_cached()[0] is True
        _db_list(db, OTHER)
        clock.t += 299
        assert eg.egress_allowed_cached()[0] is True  # still within window
        clock.t += 2
        assert eg.egress_allowed_cached() == (False, FIVE_G, "not_allowed")


class TestCachedPath:
    def test_allow_list_alone_does_not_short_circuit(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed_cached() == (False, OTHER, "not_allowed")

    def test_db_allow_list_alone_does_not_short_circuit(self, monkeypatch, db):
        _db_list(db, FIVE_G)
        _use(monkeypatch, OTHER)
        assert eg.egress_allowed_cached() == (False, OTHER, "not_allowed")

    def test_cache_tuple_shape_kept(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", FIVE_G)
        _use(monkeypatch, FIVE_G)
        eg.egress_allowed_cached()
        assert eg._cache is not None and eg._cache[1][1] == FIVE_G


class TestVerifierRegressions:
    def test_db_error_after_a_good_read_keeps_the_last_db_value(self, monkeypatch, db, _iso):
        _db_list(db, "5.5.5.5")
        _use(monkeypatch, "5.5.5.5")
        assert eg.egress_allowed()[0] is True

        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(eg, "_session_factory", broken)
        _use(monkeypatch, "8.8.8.8")
        assert eg.egress_allowed() == (False, "8.8.8.8", "not_allowed")
        clock, _ = _iso
        clock.t += 301
        assert eg.egress_allowed_cached() == (False, "8.8.8.8", "not_allowed")

    def test_db_recovers_after_error(self, monkeypatch, db):
        _db_list(db, "5.5.5.5")
        eg.egress_allowed()

        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(eg, "_session_factory", broken)
        eg.egress_allowed()
        monkeypatch.setattr(eg, "_session_factory", TestingSessionLocal)
        _db_list(db, "8.8.8.8")
        _use(monkeypatch, "8.8.8.8")
        assert eg.egress_allowed()[0] is True

    def test_ipv4_mapped_matches_allow_entry(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", "5.5.5.5")
        _use(monkeypatch, "::ffff:5.5.5.5")
        assert eg.egress_allowed() == (True, "5.5.5.5", "allowed")

    def test_ipv4_mapped_refused_by_deny_entry(self, monkeypatch):
        monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", "5.5.5.5")
        _use(monkeypatch, "::ffff:5.5.5.5")
        assert eg.egress_allowed() == (False, "5.5.5.5", "forbidden")

    def test_cidr_matches_only_inside_range(self, monkeypatch):
        monkeypatch.setattr(settings, "ALLOWED_EGRESS_IPS", "5.5.5.0/24")
        _use(monkeypatch, "::ffff:5.5.5.77")
        assert eg.egress_allowed()[0] is True
        _use(monkeypatch, "::ffff:5.5.6.1")
        assert eg.egress_allowed()[0] is False
