"""Egress guard: never scrape PJUD from a forbidden (office) IP. No real network."""
import pytest

from app.config import settings
from app.services import egress_guard as eg

OFFICE = "186.10.251.189"
HOME = "200.1.2.3"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    eg.reset_state()
    monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", OFFICE)
    clock = Clock()
    monkeypatch.setattr(eg, "EGRESS_CLOCK", clock)
    sent: list[str] = []
    monkeypatch.setattr(eg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(eg, "_default_fetchers", lambda: [_boom])
    yield clock, sent
    eg.reset_state()


def _boom(timeout):
    raise AssertionError("real network lookup attempted")


def _ip(value):
    def fetch(timeout):
        return value

    return fetch


def _use(monkeypatch, value):
    monkeypatch.setattr(eg, "_default_fetchers", lambda: [_ip(value)])


class TestSetting:
    def test_default_is_the_office_ip(self):
        from app.config import Settings

        assert Settings.model_fields["FORBIDDEN_EGRESS_IPS"].default == OFFICE

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", set()),
            ("   ", set()),
            (" , ,", set()),
            (OFFICE, {OFFICE}),
            (f" {OFFICE} , 1.1.1.1 ,, 2.2.2.2 ", {OFFICE, "1.1.1.1", "2.2.2.2"}),
        ],
    )
    def test_parse(self, raw, expected):
        assert eg.parse_forbidden_ips(raw) == expected


class TestCurrentEgressIp:
    def test_first_valid_wins(self):
        assert eg.current_egress_ip(fetchers=[_ip("1.2.3.4\n"), _ip("5.6.7.8")]) == "1.2.3.4"

    def test_falls_back_on_error_and_garbage(self):
        def err(timeout):
            raise OSError("down")

        calls = []

        def second(timeout):
            calls.append(timeout)
            return "2001:db8::1"

        got = eg.current_egress_ip(
            fetchers=[err, _ip("<html>nope</html>"), _ip(""), second], timeout=7
        )
        assert got == "2001:db8::1"
        assert calls == [7]

    def test_none_when_all_fail(self):
        assert eg.current_egress_ip(fetchers=[_ip("garbage"), _ip(None)]) is None

    def test_default_fetchers_order_is_ipify_then_ifconfig(self):
        monkey_urls = []

        class R:
            text = "1.1.1.1"

            def raise_for_status(self):
                pass

        import httpx

        orig = httpx.get
        httpx.get = lambda url, timeout=None, **k: monkey_urls.append(url) or R()
        try:
            fetchers = eg._real_fetchers()
            for f in fetchers:
                f(3)
        finally:
            httpx.get = orig
        assert monkey_urls == ["https://api.ipify.org", "https://ifconfig.me/ip"]


class TestEgressAllowed:
    def test_empty_setting_means_no_lookup_at_all(self, monkeypatch):
        monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", "")
        allowed, ip, reason = eg.egress_allowed()  # _boom would raise on lookup
        assert allowed is True and ip is None

    def test_allowed_ip(self, monkeypatch):
        _use(monkeypatch, HOME)
        assert eg.egress_allowed() == (True, HOME, "allowed")

    def test_forbidden_ip(self, monkeypatch):
        _use(monkeypatch, OFFICE)
        allowed, ip, reason = eg.egress_allowed()
        assert (allowed, ip, reason) == (False, OFFICE, "forbidden")

    def test_fail_closed_when_unknown(self, monkeypatch):
        _use(monkeypatch, "garbage")
        assert eg.egress_allowed() == (False, None, "unknown")


class TestCachedCheck:
    def test_off_means_no_lookup(self, monkeypatch):
        monkeypatch.setattr(settings, "FORBIDDEN_EGRESS_IPS", "")
        assert eg.egress_allowed_cached()[0] is True

    def test_cached_for_five_minutes(self, monkeypatch, _isolated):
        clock, _ = _isolated
        n = []

        def fetch(timeout):
            n.append(1)
            return HOME

        monkeypatch.setattr(eg, "_default_fetchers", lambda: [fetch])
        eg.egress_allowed_cached()
        clock.t += 299
        eg.egress_allowed_cached()
        assert len(n) == 1
        clock.t += 2
        eg.egress_allowed_cached()
        assert len(n) == 2

    def test_fresh_check_seeds_the_cache(self, monkeypatch, _isolated):
        _use(monkeypatch, HOME)
        eg.egress_allowed()
        monkeypatch.setattr(eg, "_default_fetchers", lambda: [_boom])
        assert eg.egress_allowed_cached() == (True, HOME, "allowed")

    def test_forbidden_is_noticed_after_expiry(self, monkeypatch, _isolated):
        clock, _ = _isolated
        _use(monkeypatch, HOME)
        assert eg.egress_allowed_cached()[0] is True
        _use(monkeypatch, OFFICE)
        assert eg.egress_allowed_cached()[0] is True  # still cached
        clock.t += 301
        assert eg.egress_allowed_cached() == (False, OFFICE, "forbidden")


class TestAlerts:
    def test_forbidden_alerts_once_with_exact_text(self, monkeypatch, _isolated):
        clock, sent = _isolated
        _use(monkeypatch, OFFICE)
        eg.egress_allowed()
        eg.egress_allowed()
        assert sent == [
            f"El worker se detuvo: está saliendo a internet por una red no permitida "
            f"(IP {OFFICE}). No consultará el PJUD hasta volver a una red permitida."
        ]

    def test_unknown_message(self, monkeypatch, _isolated):
        _, sent = _isolated
        _use(monkeypatch, None)
        eg.egress_allowed()
        assert sent == [
            "No se pudo verificar la IP de salida; el worker espera antes de consultar el PJUD."
        ]

    def test_repeats_after_one_hour_only(self, monkeypatch, _isolated):
        clock, sent = _isolated
        _use(monkeypatch, OFFICE)
        eg.egress_allowed()
        clock.t += 3599
        eg.egress_allowed()
        assert len(sent) == 1
        clock.t += 2
        eg.egress_allowed()
        assert len(sent) == 2

    def test_recovery_message_once(self, monkeypatch, _isolated):
        _, sent = _isolated
        _use(monkeypatch, OFFICE)
        eg.egress_allowed()
        _use(monkeypatch, HOME)
        eg.egress_allowed()
        eg.egress_allowed()
        assert sent[1:] == [f"El worker volvió a una red permitida (IP {HOME})."]

    def test_no_alert_when_always_allowed(self, monkeypatch, _isolated):
        _, sent = _isolated
        _use(monkeypatch, HOME)
        eg.egress_allowed()
        assert sent == []

    def test_notifier_failure_does_not_break_the_check(self, monkeypatch):
        _use(monkeypatch, OFFICE)

        def bad(text):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(eg, "send_telegram", bad)
        assert eg.egress_allowed() == (False, OFFICE, "forbidden")
