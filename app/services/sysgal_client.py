"""SysgalClient — read-only HTTP connector to the Sysgal CRM.

Only the batch endpoint ``POST {BASE}/api_sync/clientes_estado`` is used.

PRIVACY: responses carry ``nombre``/``email``/``telefono``. This module never
logs a response body and never puts one in an exception message — only the
HTTP status code and the number of RUTs involved.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Hard cap enforced by Sysgal (HTTP 400 beyond it).
MAX_RUTS_PER_REQUEST = 100

_ESTADO_PATH = "/api_sync/clientes_estado"


class SysgalError(Exception):
    """Non-200 answer, transport failure after retry, or malformed body."""


class SysgalClient:
    """Thin, synchronous client. ``transport`` is injectable for tests."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self._transport = transport

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def estado_por_ruts(self, ruts: list[str]) -> dict[str, dict]:
        """Look up up to 100 RUTs; returns the ``data`` mapping keyed exactly as sent.

        Retries ONCE on connection error / 5xx. Raises ``ValueError`` when
        ``ruts`` exceeds the cap and ``SysgalError`` on any other failure.
        """
        if len(ruts) > MAX_RUTS_PER_REQUEST:
            raise ValueError(
                f"Sysgal accepts at most {MAX_RUTS_PER_REQUEST} RUTs per request, got {len(ruts)}"
            )
        if not ruts:
            return {}

        url = f"{self.base_url}{_ESTADO_PATH}"
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        payload = {"ruts": list(ruts)}

        last_error: Optional[str] = None
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as http:
                    response = http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"transport error ({type(exc).__name__})"
                logger.warning(
                    "Sysgal request failed (attempt %d/2, %d ruts): %s",
                    attempt, len(ruts), last_error,
                )
                continue

            if 500 <= response.status_code < 600:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "Sysgal answered %s (attempt %d/2, %d ruts)",
                    last_error, attempt, len(ruts),
                )
                continue

            if response.status_code != 200:
                raise SysgalError(
                    f"Sysgal answered HTTP {response.status_code} for {len(ruts)} ruts"
                )

            return self._parse(response, len(ruts))

        raise SysgalError(f"Sysgal unavailable after retry ({last_error}) for {len(ruts)} ruts")

    @staticmethod
    def _parse(response: httpx.Response, count: int) -> dict[str, dict]:
        try:
            body = response.json()
        except ValueError as exc:
            raise SysgalError(f"Sysgal returned a non-JSON body (HTTP 200, {count} ruts)") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise SysgalError(f"Sysgal body has no 'data' mapping (HTTP 200, {count} ruts)")
        return data
