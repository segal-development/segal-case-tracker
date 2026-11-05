"""TEMPORARY diagnostic probe: which layer fails on ERR_NAME_NOT_RESOLVED?

REMOVE this module (and its single call site in ``base.py``) once the question
below is answered. Pure observability: it never retries, never raises and never
changes what the caller does.

Context: ``page.goto(host)`` fails with ``net::ERR_NAME_NOT_RESOLVED`` in
bursts of minutes while an in-page ``fetch(host)`` against the same host works.
When a navigation fails that way we emit ONE line, at that very instant::

    DNS_RESOLUTION_PROBE host=<h> getaddrinfo=<ok|fail:<Exc>|timeout>
        getaddrinfo_ip=<ip|-> getaddrinfo_ms=<n> public_8888=<ok|fail:<Exc>|timeout>
        public_8888_ips=<ip,ip|-> public_8888_ms=<n>

(one physical line). How to read it:

* ``getaddrinfo`` AND ``public_8888`` both fail  -> a real DNS/network outage.
* ``getaddrinfo`` fails, ``public_8888`` works    -> macOS ``mDNSResponder`` (the
  OS resolver Chromium uses) is the culprit; a direct query to 8.8.8.8 bypasses
  it, which is why ``dig @8.8.8.8`` spot checks looked healthy.
* both work                                      -> the failure is inside
  Chromium, above name resolution.
"""

import asyncio
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import urlparse

from app.scrapper.pjud.dns_resolver import (
    build_a_query as _build_a_query,
    parse_a_records as _parse_a_records,
    resolve_via_public_dns as _resolve_via_public,
)

logger = logging.getLogger(__name__)

#: At most one probe per host in this window. Failure bursts produce hundreds
#: of errors over ~16 minutes; this keeps the log readable (a handful of lines).
PROBE_MIN_INTERVAL_SECONDS = 30.0

#: Each resolver gets this long; a hung resolver must never stall anything.
PROBE_TIMEOUT_SECONDS = 3.0

_MARKER = "ERR_NAME_NOT_RESOLVED"

_clock: Callable[[], float] = time.monotonic
_last_probe: dict[str, float] = {}
#: Strong references so fire-and-forget tasks are not garbage collected.
_pending: set = set()
#: Dedicated threads: a resolver that hangs must not starve the default executor.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-probe")


def _resolve_via_os(host: str) -> str:
    """The path Chromium uses: the OS resolver. Returns the first IP."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return infos[0][4][0]


async def _timed(fn: Callable, host: str):
    """Run a blocking resolver bounded by PROBE_TIMEOUT_SECONDS.

    Returns ``(status, value, elapsed_ms)``; never raises.
    """
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        value = await asyncio.wait_for(
            loop.run_in_executor(_executor, fn, host), PROBE_TIMEOUT_SECONDS
        )
        status = "ok"
    except asyncio.TimeoutError:
        value, status = None, "timeout"
    except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
        value, status = None, f"fail:{type(exc).__name__}"
    return status, value, int((time.monotonic() - started) * 1000)


async def _run_probe(host: str) -> None:
    try:
        (os_status, os_ip, os_ms), (pub_status, pub_ips, pub_ms) = await asyncio.gather(
            _timed(_resolve_via_os, host), _timed(_resolve_via_public, host)
        )
        logger.info(
            "DNS_RESOLUTION_PROBE host=%s getaddrinfo=%s getaddrinfo_ip=%s "
            "getaddrinfo_ms=%d public_8888=%s public_8888_ips=%s public_8888_ms=%d",
            host, os_status, os_ip or "-", os_ms,
            pub_status, ",".join(pub_ips) if pub_ips else "-", pub_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("DNS_RESOLUTION_PROBE_ERROR host=%s error=%r", host, exc)


def _host_of(url: Optional[str]) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def maybe_probe_name_resolution(error_text: Optional[str], url: Optional[str]) -> None:
    """Schedule one rate-limited probe when *error_text* is ERR_NAME_NOT_RESOLVED.

    Fire-and-forget: returns immediately, so the caller's timing and outcome are
    untouched. Never raises.
    """
    try:
        if not error_text or _MARKER not in error_text:
            return
        host = _host_of(url)
        if not host:
            return
        now = _clock()
        last = _last_probe.get(host)
        if last is not None and now - last < PROBE_MIN_INTERVAL_SECONDS:
            return
        loop = asyncio.get_running_loop()
        _last_probe[host] = now
        task = loop.create_task(_run_probe(host))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception as exc:  # noqa: BLE001
        try:
            logger.info("DNS_RESOLUTION_PROBE_ERROR error=%r", exc)
        except Exception:  # noqa: BLE001
            pass
