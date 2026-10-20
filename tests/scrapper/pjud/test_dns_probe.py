"""TEMPORARY diagnostic probe: DNS_RESOLUTION_PROBE.

Pure observability. These tests pin that the probe fires only on
ERR_NAME_NOT_RESOLVED, is rate-limited per host, and can never break or stall
the caller. No test touches the real network: both resolvers are stubbed.
"""

import asyncio
import logging
import struct
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.scrapper.pjud import dns_probe

INDEX_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
HOST = "oficinajudicialvirtual.pjud.cl"
NAV_ERR = f"Page.goto: net::ERR_NAME_NOT_RESOLVED at {INDEX_URL}"


class _ListHandler(logging.Handler):
    """Direct handler: caplog is unreliable here (alembic fileConfig disables loggers)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def logs():
    logger = logging.getLogger("app.scrapper.pjud.dns_probe")
    handler = _ListHandler()
    was_disabled, old_level = logger.disabled, logger.level
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    dns_probe._last_probe.clear()
    yield handler.messages
    logger.removeHandler(handler)
    logger.disabled, logger.level = was_disabled, old_level
    dns_probe._last_probe.clear()


@pytest.fixture
def resolvers(monkeypatch):
    monkeypatch.setattr(dns_probe, "_resolve_via_os", lambda host: "203.0.113.7")
    monkeypatch.setattr(dns_probe, "_resolve_via_public", lambda host: ["203.0.113.7"])


async def _drain():
    if dns_probe._pending:
        await asyncio.gather(*list(dns_probe._pending), return_exceptions=True)


def _probe_lines(messages):
    return [m for m in messages if m.startswith("DNS_RESOLUTION_PROBE ")]


class TestFires:
    async def test_fires_once_on_name_not_resolved(self, logs, resolvers):
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        await _drain()
        lines = _probe_lines(logs)
        assert len(lines) == 1
        line = lines[0]
        assert f"host={HOST}" in line
        assert "getaddrinfo=ok" in line
        assert "getaddrinfo_ip=203.0.113.7" in line
        assert "getaddrinfo_ms=" in line
        assert "public_8888=ok" in line
        assert "public_8888_ips=203.0.113.7" in line
        assert "public_8888_ms=" in line

    async def test_reports_os_failure_with_public_success(self, logs, monkeypatch):
        import socket

        def boom(host):
            raise socket.gaierror(8, "nodename nor servname provided")

        monkeypatch.setattr(dns_probe, "_resolve_via_os", boom)
        monkeypatch.setattr(dns_probe, "_resolve_via_public", lambda h: ["203.0.113.7"])
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        await _drain()
        line = _probe_lines(logs)[0]
        assert "getaddrinfo=fail:gaierror" in line
        assert "public_8888=ok" in line

    async def test_does_not_fire_on_other_errors(self, logs, resolvers):
        for text in (
            "net::ERR_CONNECTION_RESET at x",
            "Timeout 30000ms exceeded",
            "",
        ):
            dns_probe.maybe_probe_name_resolution(text, INDEX_URL)
        await _drain()
        assert _probe_lines(logs) == []
        assert not dns_probe._pending


class TestRateLimit:
    async def test_second_failure_inside_window_is_skipped(self, logs, resolvers, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(dns_probe, "_clock", lambda: now[0])
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        now[0] += dns_probe.PROBE_MIN_INTERVAL_SECONDS - 1
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        await _drain()
        assert len(_probe_lines(logs)) == 1

    async def test_fires_again_after_window(self, logs, resolvers, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(dns_probe, "_clock", lambda: now[0])
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        now[0] += dns_probe.PROBE_MIN_INTERVAL_SECONDS + 1
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        await _drain()
        assert len(_probe_lines(logs)) == 2

    async def test_window_is_per_host(self, logs, resolvers):
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        dns_probe.maybe_probe_name_resolution(NAV_ERR, "https://other.example.cl/x")
        await _drain()
        assert len(_probe_lines(logs)) == 2


class TestNeverBreaksTheCaller:
    async def test_raising_resolvers_are_reported_not_raised(self, logs, monkeypatch):
        def boom(host):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(dns_probe, "_resolve_via_os", boom)
        monkeypatch.setattr(dns_probe, "_resolve_via_public", boom)
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)  # must not raise
        await _drain()
        line = _probe_lines(logs)[0]
        assert "getaddrinfo=fail:RuntimeError" in line
        assert "public_8888=fail:RuntimeError" in line

    async def test_hanging_resolvers_time_out_and_caller_returns_at_once(
        self, logs, monkeypatch
    ):
        monkeypatch.setattr(dns_probe, "PROBE_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(dns_probe, "_resolve_via_os", lambda h: time.sleep(1.0))
        monkeypatch.setattr(dns_probe, "_resolve_via_public", lambda h: time.sleep(1.0))
        started = time.monotonic()
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)
        assert time.monotonic() - started < 0.05  # fire-and-forget: caller never waits
        await _drain()
        assert time.monotonic() - started < 0.8  # bounded by the timeout, not the hang
        line = _probe_lines(logs)[0]
        assert "getaddrinfo=timeout" in line
        assert "public_8888=timeout" in line

    async def test_internal_failure_is_logged_not_raised(self, logs, monkeypatch):
        monkeypatch.setattr(dns_probe, "_clock", lambda: (_ for _ in ()).throw(ValueError("clock")))
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)  # must not raise
        assert any(m.startswith("DNS_RESOLUTION_PROBE_ERROR") for m in logs)

    def test_no_running_loop_is_swallowed(self, logs, resolvers):
        dns_probe.maybe_probe_name_resolution(NAV_ERR, INDEX_URL)  # sync test: no loop
        assert any(m.startswith("DNS_RESOLUTION_PROBE_ERROR") for m in logs)


class TestDnsWireFormat:
    def test_query_and_parse_roundtrip(self):
        query = dns_probe._build_a_query(HOST, txid=0x1234)
        assert query[:2] == b"\x12\x34"
        assert query.endswith(b"\x00\x00\x01\x00\x01")
        # Fake response: echo question, one A answer using a compression pointer.
        header = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0)
        question = query[12:]
        answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([203, 0, 113, 9])
        assert dns_probe._parse_a_records(header + question + answer, 0x1234) == ["203.0.113.9"]

    def test_nonzero_rcode_raises(self):
        query = dns_probe._build_a_query(HOST, txid=1)
        header = struct.pack(">HHHHHH", 1, 0x8183, 1, 0, 0, 0)  # NXDOMAIN
        with pytest.raises(OSError, match="rcode=3"):
            dns_probe._parse_a_records(header + query[12:], 1)


class TestHookInMisCausasLoad:
    """The choke point: _load_index_and_probe_miscausas catches the goto failure."""

    async def test_goto_name_not_resolved_triggers_probe_and_still_returns_error_text(self):
        from app.scrapper.pjud.civil import CivilScraper

        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception(NAV_ERR))
        with patch("app.scrapper.pjud.base.maybe_probe_name_resolution") as probe:
            result = await scraper._load_index_and_probe_miscausas(page, navigate=True)
        assert result == (False, NAV_ERR)  # outcome unchanged
        probe.assert_called_once()
        assert probe.call_args.args[0] == NAV_ERR

    async def test_other_goto_error_does_not_trigger_probe_effects(self, logs, resolvers):
        from app.scrapper.pjud.civil import CivilScraper

        scraper = CivilScraper(headless=True)
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("Timeout 30000ms exceeded"))
        result = await scraper._load_index_and_probe_miscausas(page, navigate=True)
        await _drain()
        assert result == (False, "Timeout 30000ms exceeded")
        assert _probe_lines(logs) == []
