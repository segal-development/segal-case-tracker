"""Egress guard: the worker must never scrape PJUD from a forbidden IP.

The office IP was blocked by PJUD once because the station scraped from the
office network. The guard asks public IP echo services where we exit to the
internet and refuses to scrape when that IP is in ``FORBIDDEN_EGRESS_IPS``.
It FAILS CLOSED: if the IP cannot be determined, scraping is not allowed.

Lookups are blocking (httpx); call from async code through ``asyncio.to_thread``.
"""

import ipaddress
import logging
import time
from typing import Callable, Optional

import httpx

from app.config import settings
from app.services.telegram_notifier import send_telegram

logger = logging.getLogger(__name__)

#: Prefix of the error recorded when a lawyer is stopped by the guard.
EGRESS_REASON_PREFIX = "Red no permitida"

#: Seconds a lookup stays valid for ``egress_allowed_cached`` (detail loop).
CACHE_SECONDS = 5 * 60
#: Minimum seconds between two repeats of the same alert while blocked.
ALERT_REPEAT_SECONDS = 60 * 60

#: Monotonic clock for the cache and the alert throttle; monkeypatch in tests.
EGRESS_CLOCK: Callable[[], float] = time.monotonic

_IP_ECHO_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip")

Fetcher = Callable[[float], Optional[str]]

_cache: Optional[tuple[float, tuple[bool, Optional[str], str]]] = None
_blocked = False
_last_alert_at: Optional[float] = None


def reset_state() -> None:
    """Forget the cache and the alert state (tests)."""
    global _cache, _blocked, _last_alert_at
    _cache = None
    _blocked = False
    _last_alert_at = None


def parse_forbidden_ips(raw: str) -> set[str]:
    """Comma-separated setting -> set of IPs; blanks ignored."""
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def _normalize(value: Optional[str]) -> Optional[str]:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def _real_fetchers() -> list[Fetcher]:
    def make(url: str) -> Fetcher:
        def fetch(timeout: float) -> Optional[str]:
            response = httpx.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text

        return fetch

    return [make(url) for url in _IP_ECHO_URLS]


def _default_fetchers() -> list[Fetcher]:
    return _real_fetchers()


def current_egress_ip(
    fetchers: Optional[list[Fetcher]] = None, timeout: float = 10
) -> Optional[str]:
    """First valid IPv4/IPv6 answered by the echo services, in order; else None."""
    for fetch in fetchers if fetchers is not None else _default_fetchers():
        try:
            ip = _normalize(fetch(timeout))
        except Exception as exc:  # noqa: BLE001 — any failure means "try the next one"
            logger.warning("Egress IP lookup failed: %s", type(exc).__name__)
            continue
        if ip:
            return ip
    return None


def _alert(text: str) -> None:
    try:
        send_telegram(text)
    except Exception:  # noqa: BLE001 — an alert must never break the worker
        logger.warning("Egress guard alert could not be sent")


def _track(allowed: bool, ip: Optional[str]) -> None:
    global _blocked, _last_alert_at
    now = EGRESS_CLOCK()
    if allowed:
        if _blocked:
            _blocked = False
            _last_alert_at = None
            _alert(f"El worker volvió a una red permitida (IP {ip}).")
        return
    due = _last_alert_at is None or now - _last_alert_at >= ALERT_REPEAT_SECONDS
    _blocked = True
    if due:
        _last_alert_at = now
        if ip:
            _alert(
                "El worker se detuvo: está saliendo a internet por una red no "
                f"permitida (IP {ip}). No consultará el PJUD hasta volver a una "
                "red permitida."
            )
        else:
            _alert(
                "No se pudo verificar la IP de salida; el worker espera antes de "
                "consultar el PJUD."
            )


def egress_allowed() -> tuple[bool, Optional[str], str]:
    """Fresh check -> (allowed, ip, reason). Guard off => (True, None, "off")."""
    global _cache
    forbidden = {_normalize(x) or x for x in parse_forbidden_ips(settings.FORBIDDEN_EGRESS_IPS)}
    if not forbidden:
        return True, None, "off"
    ip = current_egress_ip()
    if ip is None:
        result = (False, None, "unknown")
        logger.warning("Egress guard: egress IP unknown; failing closed")
    elif ip in forbidden:
        result = (False, ip, "forbidden")
        logger.warning("Egress guard: egress IP %s is forbidden", ip)
    else:
        result = (True, ip, "allowed")
        logger.info("Egress guard: egress IP %s allowed", ip)
    _cache = (EGRESS_CLOCK(), result)
    _track(result[0], ip)
    return result


def egress_allowed_cached() -> tuple[bool, Optional[str], str]:
    """Like ``egress_allowed`` but reuses a result younger than 5 minutes."""
    if not parse_forbidden_ips(settings.FORBIDDEN_EGRESS_IPS):
        return True, None, "off"
    if _cache is not None and EGRESS_CLOCK() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    return egress_allowed()
