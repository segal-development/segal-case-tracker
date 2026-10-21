"""Resolve the PJUD host ourselves and hand Chromium the address at launch.

WHY THIS EXISTS
---------------
On this station the macOS system resolver — ``getaddrinfo``, the path Chromium
uses — *refuses* the PJUD host in 2-4 ms (an immediate failure, not a timeout)
while a direct UDP query to a public resolver answers the same host at the same
instant. Measured 8/8 times by ``dns_probe``::

    DNS_RESOLUTION_PROBE host=oficinajudicialvirtual.pjud.cl
      getaddrinfo=fail:gaierror getaddrinfo_ms=2..4
      public_8888=ok public_8888_ips=200.10.184.62 public_8888_ms=5..15

The visible cost is a burst of ``net::ERR_NAME_NOT_RESOLVED`` navigations
(489 one day, 32 the next). Restarting ``mDNSResponder`` and rebooting the
machine did not stop it, and it happens on two different networks.

WHAT IT DOES
------------
When ``PJUD_BYPASS_SYSTEM_DNS`` is on, we resolve the host with our own UDP
query before the browser starts and pass Chromium exactly one extra argument::

    --host-resolver-rules=MAP <host> <ip>

Chromium then skips name resolution *for that host only*. Everything else about
the browser is untouched: the request still carries the real ``Host`` header and
the real SNI, and the certificate is still validated against the real hostname,
so neither PJUD nor F5 Shape sees a different connection. That narrowness is the
whole reason this was chosen over DNS-over-HTTPS, which would reroute *all* of
the browser's name resolution through a third party.

IT FAILS OPEN, ALWAYS
---------------------
If our own resolution fails, times out, or returns something that is not an
IPv4 address, we return no argument and the browser launches exactly as it does
today. A browser that cannot reach PJUD is worse than the failure we are fixing.

MAPPED ADDRESSES ARE NEVER HARDCODED
------------------------------------
PJUD is load balanced and has answered with more than one address. The mapping
is resolved fresh on every browser launch and lives only as long as that
browser. If the pinned address stops serving mid-session, the failure shows up
as a connection error (not ``ERR_NAME_NOT_RESOLVED``), and the scraper's
existing re-auth path builds a brand-new browser, which resolves again and
picks up whichever address the load balancer is serving then.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import struct
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)

#: The only host we map. Everything else keeps using the system resolver.
PJUD_HOST = "oficinajudicialvirtual.pjud.cl"

#: Public resolver queried directly over UDP, bypassing the OS entirely.
PUBLIC_RESOLVER = "8.8.8.8"

#: Hard bound on the extra latency this adds to a browser launch. Measured
#: answers come back in 5-15 ms; anything slower is treated as unavailable.
RESOLVE_TIMEOUT_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Minimal stdlib DNS client (A records over UDP). No third-party dependency,
# and — the point of the exercise — no call into the system resolver.
# ---------------------------------------------------------------------------


def build_a_query(host: str, txid: int) -> bytes:
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(p)]) + p.encode("ascii") for p in host.rstrip(".").split(".")
    ) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        length = data[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + length


def parse_a_records(data: bytes, txid: int) -> List[str]:
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != txid:
        raise OSError("dns txid mismatch")
    rcode = flags & 0x000F
    if rcode != 0:
        raise OSError(f"dns rcode={rcode}")
    pos = 12
    for _ in range(qd):
        pos = _skip_name(data, pos) + 4
    ips = []
    for _ in range(an):
        pos = _skip_name(data, pos)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[pos:pos + 4]))
        pos += rdlen
    if not ips:
        raise OSError("dns answer has no A records")
    return ips


def resolve_via_public_dns(
    host: str,
    resolver: str = PUBLIC_RESOLVER,
    timeout: float = RESOLVE_TIMEOUT_SECONDS,
) -> List[str]:
    """Direct UDP A query, bypassing the OS resolver. Blocking; raises on failure."""
    txid = int.from_bytes(os.urandom(2), "big")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(build_a_query(host, txid), (resolver, 53))
        data, _ = sock.recvfrom(2048)
    finally:
        sock.close()
    return parse_a_records(data, txid)


# ---------------------------------------------------------------------------
# The launch argument
# ---------------------------------------------------------------------------


async def chromium_dns_bypass_args(host: str = PJUD_HOST) -> List[str]:
    """Chromium arguments that map *host* to an address we resolved ourselves.

    Returns ``[]`` — today's behaviour, launching with the system resolver —
    when the setting is off or when anything at all goes wrong.
    """
    if not settings.PJUD_BYPASS_SYSTEM_DNS:
        return []

    timeout = RESOLVE_TIMEOUT_SECONDS
    try:
        ips = await asyncio.wait_for(
            asyncio.to_thread(resolve_via_public_dns, host), timeout
        )
        ip = str(ipaddress.IPv4Address((ips or [""])[0]))
    except Exception as exc:  # noqa: BLE001 - fail open, never closed
        logger.warning(
            "CHROMIUM_DNS_BYPASS_UNAVAILABLE host=%s resolver=%s error=%r "
            "-- launching with the system resolver",
            host, PUBLIC_RESOLVER, exc,
        )
        return []

    logger.info(
        "CHROMIUM_DNS_BYPASS_ACTIVE host=%s ip=%s resolver=%s",
        host, ip, PUBLIC_RESOLVER,
    )
    return [f"--host-resolver-rules=MAP {host} {ip}"]
