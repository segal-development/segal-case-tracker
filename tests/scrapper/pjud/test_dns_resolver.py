"""Launch-time DNS bypass for Chromium: the resolver and the launch argument.

Context: on this station ``getaddrinfo`` — the path Chromium uses on macOS —
refuses the PJUD host in 2-4 ms while a direct UDP query to a public resolver
answers the same host at the same instant (see ``dns_probe``'s
``DNS_RESOLUTION_PROBE`` evidence). These tests pin the mechanism that hands
Chromium an address we resolved ourselves, and — just as important — pin that
it FAILS OPEN: anything that goes wrong leaves the browser launching exactly
as it does today.

No test touches the real network: the UDP resolver is always stubbed.
"""

import asyncio
import logging
import struct
from pathlib import Path

import pytest

from app.config import settings
from app.scrapper.pjud import dns_resolver

HOST = "oficinajudicialvirtual.pjud.cl"

#: The two addresses PJUD's load balancer actually answered during the probe
#: campaign. They must never appear in the source: the IP has to come from our
#: own query, every launch.
OBSERVED_PJUD_IPS = ("200.10.184.62", "200.10.184.125")


class _ListHandler(logging.Handler):
    """Direct handler: caplog is unreliable here (alembic fileConfig disables loggers)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def logs():
    logger = logging.getLogger("app.scrapper.pjud.dns_resolver")
    handler = _ListHandler()
    was_disabled, old_level = logger.disabled, logger.level
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield handler.messages
    logger.removeHandler(handler)
    logger.disabled, logger.level = was_disabled, old_level


@pytest.fixture
def bypass_on(monkeypatch):
    monkeypatch.setattr(settings, "PJUD_BYPASS_SYSTEM_DNS", True)


@pytest.fixture
def bypass_off(monkeypatch):
    monkeypatch.setattr(settings, "PJUD_BYPASS_SYSTEM_DNS", False)


def _stub_resolver(monkeypatch, ips):
    calls = []

    def fake(host, **kwargs):
        calls.append(host)
        return list(ips)

    monkeypatch.setattr(dns_resolver, "resolve_via_public_dns", fake)
    return calls


class TestSettingGate:
    async def test_default_is_off(self):
        """Shape risk: the station enables this by hand, it is never the default."""
        assert settings.PJUD_BYPASS_SYSTEM_DNS is False

    async def test_no_argument_when_setting_is_off(self, bypass_off, monkeypatch):
        calls = _stub_resolver(monkeypatch, ["203.0.113.7"])
        assert await dns_resolver.chromium_dns_bypass_args(HOST) == []
        assert calls == []  # off means we do not even resolve

    async def test_argument_when_setting_is_on(self, bypass_on, monkeypatch):
        _stub_resolver(monkeypatch, ["203.0.113.7"])
        args = await dns_resolver.chromium_dns_bypass_args(HOST)
        assert args == [f"--host-resolver-rules=MAP {HOST} 203.0.113.7"]


class TestAddressComesFromOurOwnQuery:
    async def test_uses_the_ip_our_resolver_returned(self, bypass_on, monkeypatch):
        calls = _stub_resolver(monkeypatch, ["198.51.100.23", "198.51.100.24"])
        args = await dns_resolver.chromium_dns_bypass_args(HOST)
        assert args == [f"--host-resolver-rules=MAP {HOST} 198.51.100.23"]
        assert calls == [HOST]

    async def test_resolves_again_on_every_launch(self, bypass_on, monkeypatch):
        """PJUD is load balanced: a pinned address must not outlive one browser."""
        answers = [["198.51.100.1"], ["198.51.100.2"]]
        monkeypatch.setattr(
            dns_resolver, "resolve_via_public_dns", lambda host, **kw: answers.pop(0)
        )
        first = await dns_resolver.chromium_dns_bypass_args(HOST)
        second = await dns_resolver.chromium_dns_bypass_args(HOST)
        assert first == [f"--host-resolver-rules=MAP {HOST} 198.51.100.1"]
        assert second == [f"--host-resolver-rules=MAP {HOST} 198.51.100.2"]

    def test_no_pjud_address_is_hardcoded_in_the_module(self):
        source = Path(dns_resolver.__file__).read_text(encoding="utf-8")
        for ip in OBSERVED_PJUD_IPS:
            assert f'"{ip}"' not in source and f"'{ip}'" not in source


class TestFailsOpen:
    async def test_resolution_error_falls_back_to_no_argument(self, bypass_on, monkeypatch, logs):
        def boom(host, **kwargs):
            raise OSError("dns rcode=2")

        monkeypatch.setattr(dns_resolver, "resolve_via_public_dns", boom)
        assert await dns_resolver.chromium_dns_bypass_args(HOST) == []
        assert any(m.startswith("CHROMIUM_DNS_BYPASS_UNAVAILABLE") for m in logs)

    async def test_resolution_timeout_falls_back_to_no_argument(self, bypass_on, monkeypatch, logs):
        monkeypatch.setattr(dns_resolver, "RESOLVE_TIMEOUT_SECONDS", 0.05)

        def hang(host, **kwargs):
            import time

            time.sleep(1.0)
            return ["203.0.113.7"]

        monkeypatch.setattr(dns_resolver, "resolve_via_public_dns", hang)
        assert await dns_resolver.chromium_dns_bypass_args(HOST) == []
        assert any(m.startswith("CHROMIUM_DNS_BYPASS_UNAVAILABLE") for m in logs)

    async def test_empty_answer_falls_back_to_no_argument(self, bypass_on, monkeypatch, logs):
        monkeypatch.setattr(dns_resolver, "resolve_via_public_dns", lambda host, **kw: [])
        assert await dns_resolver.chromium_dns_bypass_args(HOST) == []
        assert any(m.startswith("CHROMIUM_DNS_BYPASS_UNAVAILABLE") for m in logs)

    async def test_garbage_answer_falls_back_to_no_argument(self, bypass_on, monkeypatch, logs):
        """A value that is not a dotted-quad must never reach the command line."""
        monkeypatch.setattr(
            dns_resolver, "resolve_via_public_dns", lambda host, **kw: ["not an ip"]
        )
        assert await dns_resolver.chromium_dns_bypass_args(HOST) == []
        assert any(m.startswith("CHROMIUM_DNS_BYPASS_UNAVAILABLE") for m in logs)


class TestObservability:
    async def test_success_logs_one_greppable_line(self, bypass_on, monkeypatch, logs):
        _stub_resolver(monkeypatch, ["203.0.113.7"])
        await dns_resolver.chromium_dns_bypass_args(HOST)
        active = [m for m in logs if m.startswith("CHROMIUM_DNS_BYPASS_ACTIVE")]
        assert len(active) == 1
        assert f"host={HOST}" in active[0]
        assert "ip=203.0.113.7" in active[0]


class TestDnsWireFormat:
    def test_query_and_parse_roundtrip(self):
        query = dns_resolver.build_a_query(HOST, txid=0x1234)
        assert query[:2] == b"\x12\x34"
        assert query.endswith(b"\x00\x00\x01\x00\x01")
        header = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0)
        question = query[12:]
        answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([203, 0, 113, 9])
        assert dns_resolver.parse_a_records(header + question + answer, 0x1234) == [
            "203.0.113.9"
        ]

    def test_nonzero_rcode_raises(self):
        query = dns_resolver.build_a_query(HOST, txid=1)
        header = struct.pack(">HHHHHH", 1, 0x8183, 1, 0, 0, 0)  # NXDOMAIN
        with pytest.raises(OSError, match="rcode=3"):
            dns_resolver.parse_a_records(header + query[12:], 1)
