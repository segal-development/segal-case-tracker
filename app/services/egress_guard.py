"""Egress guard: the worker must never scrape PJUD from a forbidden IP.

The office IP was blocked by PJUD once because the station scraped from the
office network. The guard asks public IP echo services where we exit to the
internet and refuses to scrape when that IP is in ``FORBIDDEN_EGRESS_IPS``.
Optionally an ALLOW-LIST (DB setting ``estacion_ips_permitidas``, fallback env
``ALLOWED_EGRESS_IPS``; IPs or CIDRs) restricts scraping to those networks; the
deny-list still wins. Both empty = guard off. It FAILS CLOSED: if the IP cannot be determined, scraping is not allowed.

Lookups are blocking (httpx); call from async code through ``asyncio.to_thread``.
"""

import ipaddress
import logging
import time
from typing import Callable, Optional

import httpx

from app.config import settings
from app.services import app_settings
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
#: (read_at, raw allow-list text); never older than CACHE_SECONDS.
_allow_cache: Optional[tuple[float, str]] = None
#: Last allow-list text read successfully from the DB (None = never in this process).
_last_db_raw: Optional[str] = None
_blocked = False
_last_alert_at: Optional[float] = None


def reset_state() -> None:
    """Forget the cache and the alert state (tests)."""
    global _cache, _allow_cache, _last_db_raw, _blocked, _last_alert_at
    _cache = None
    _last_db_raw = None
    _allow_cache = None
    _blocked = False
    _last_alert_at = None


def parse_forbidden_ips(raw: str) -> set[str]:
    """Comma-separated setting -> set of IPs; blanks ignored."""
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def parse_allowed_networks(raw: str) -> list:
    """Comma-separated IPs/CIDRs -> networks; blanks and invalid entries are skipped."""
    nets = []
    for part in parse_forbidden_ips(raw):
        net = parse_entry(part)
        if net is None:
            logger.warning("Egress guard: ignoring invalid allow-list entry")
            continue
        nets.append(net)
    return sorted(nets, key=lambda n: (n.version, int(n.network_address), n.prefixlen))


def parse_entry(entry: str, strict: bool = False):
    """One IP or CIDR (IPv4/IPv6) -> network, or None when invalid."""
    try:
        return ipaddress.ip_network((entry or "").strip(), strict=strict)
    except ValueError:
        return None


#: Seam for tests: opens the session used to read the DB allow-list.
def _session_factory():
    from app.core.database import SessionLocal

    return SessionLocal()


def _read_allow_raw(force: bool = False) -> str:
    """Raw allow-list: DB value if the row exists, else env.

    On a DB error the last value read successfully from the DB is reused (never the env
    fallback over it, or a DB-only list would read as empty = guard off); env is used only
    when the DB was never read in this process. Cached for at most CACHE_SECONDS; ``force``
    re-reads (fresh check).

    NOTE: a configured list whose entries are all invalid refuses everything (fail closed)
    on purpose: a typo must never silently open the guard.
    """
    global _allow_cache, _last_db_raw
    now = EGRESS_CLOCK()
    if not force and _allow_cache is not None and now - _allow_cache[0] < CACHE_SECONDS:
        return _allow_cache[1]
    raw = settings.ALLOWED_EGRESS_IPS or ""
    try:
        session = _session_factory()
        try:
            value = app_settings.get_text(session, app_settings.KEY_IPS_PERMITIDAS)
        finally:
            session.close()
        _last_db_raw = value  # None = no row yet (env applies)
        if value is not None:
            raw = value
    except Exception as exc:  # noqa: BLE001 — never crash the guard
        if _last_db_raw is not None:
            raw = _last_db_raw
        logger.warning(
            "Egress guard: allow-list DB read failed (%s); using %s value",
            type(exc).__name__,
            "last DB" if _last_db_raw is not None else "env",
        )
    _allow_cache = (now, raw)
    return raw


def _normalize(value: Optional[str]) -> Optional[str]:
    try:
        addr = ipaddress.ip_address((value or "").strip())
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped  # ::ffff:a.b.c.d is the IPv4 a.b.c.d
        return str(addr)
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


def _track(allowed: bool, ip: Optional[str], reason: str = "") -> None:
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
        if ip and reason == "not_allowed":
            _alert(
                f"El worker se detuvo: la IP {ip} no está en la lista de redes permitidas."
            )
        elif ip:
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


def _in_allow_list(ip: str, raw: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr in net for net in parse_allowed_networks(raw) if net.version == addr.version)


def egress_allowed() -> tuple[bool, Optional[str], str]:
    """Fresh check -> (allowed, ip, reason). Guard off => (True, None, "off")."""
    global _cache
    forbidden = {_normalize(x) or x for x in parse_forbidden_ips(settings.FORBIDDEN_EGRESS_IPS)}
    allow_raw = _read_allow_raw(force=True)
    allow_configured = bool(parse_forbidden_ips(allow_raw))
    if not forbidden and not allow_configured:
        return True, None, "off"
    ip = current_egress_ip()
    if ip is None:
        result = (False, None, "unknown")
        logger.warning("Egress guard: egress IP unknown; failing closed")
    elif ip in forbidden:
        result = (False, ip, "forbidden")
        logger.warning("Egress guard: egress IP %s is forbidden", ip)
    elif allow_configured and not _in_allow_list(ip, allow_raw):
        result = (False, ip, "not_allowed")
        logger.warning("Egress guard: egress IP %s is not in the allow-list", ip)
    else:
        result = (True, ip, "allowed")
        logger.info("Egress guard: egress IP %s allowed", ip)
    _cache = (EGRESS_CLOCK(), result)
    _track(result[0], ip, result[2])
    return result


def egress_allowed_cached() -> tuple[bool, Optional[str], str]:
    """Like ``egress_allowed`` but reuses a result younger than 5 minutes."""
    if not parse_forbidden_ips(settings.FORBIDDEN_EGRESS_IPS) and not parse_forbidden_ips(
        _read_allow_raw()
    ):
        return True, None, "off"
    if _cache is not None and EGRESS_CLOCK() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    return egress_allowed()
