"""SysgalClient — read-only HTTP connector to the Sysgal CRM.

ATENCION: Sysgal no tiene ambiente de pruebas. La URL configurada es
PRODUCCION y la credencial abre la base real del estudio (confirmado por su
equipo el 28-09-2026). Todo lo que se consulta aca son datos de clientes
reales y cada llamada queda en sus registros.

Endpoints used: ``POST {BASE}/api_sync/clientes_estado`` (batch coverage),
``GET {BASE}/api_sync/causas`` (paginated by id_causa) and
``GET {BASE}/api_sync/usuarios``.

PRIVACY: responses carry ``nombre``/``email``/``telefono``. This module never
logs a response body and never puts one in an exception message — only the
HTTP status code and the number of RUTs involved.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

# Hard cap enforced by Sysgal (HTTP 400 beyond it).
MAX_RUTS_PER_REQUEST = 100

# Hard cap on ``limite`` for GET /api_sync/causas (announced contract).
MAX_CAUSAS_PER_PAGE = 500
# Safety net against a cursor that never ends (400 x 500 = 200k causas). Sysgal ids reached
# ~57,800 after 55,000 causas on 2026-11-09, so 100k was too tight.
MAX_CAUSAS_PAGES = 400

_ESTADO_PATH = "/api_sync/clientes_estado"
_CAUSAS_PATH = "/api_sync/causas"
_USUARIOS_PATH = "/api_sync/usuarios"
_ESTADISTICAS_CARTERA_PATH = "/api_sync/estadisticas_cartera"
_SIN_GESTION_PATH = "/api_sync/causas_sin_gestion"
_ESCRITOS_ESTADISTICAS_PATH = "/api_sync/escritos_estadisticas"
_ESCRITOS_PATH = "/api_sync/escritos"

# Hard cap on ``limite`` for GET /api_sync/causas_sin_gestion.
MAX_SIN_GESTION_PER_PAGE = 500
MAX_ESCRITOS_PER_PAGE = 500


class SysgalError(Exception):
    """Non-200 answer, transport failure after retry, or malformed body."""


@dataclass
class SysgalCausaDTO:
    """One causa as Sysgal reports it, already mapped off the wire names."""

    sysgal_id: int
    rol: str
    tribunal: str
    rut_cliente: Optional[str] = None
    abogado_sysgal_id: Optional[int] = None
    abogado_nombre: Optional[str] = None
    etapa: Optional[str] = None
    tramite: Optional[str] = None
    updated_at: Optional[datetime] = None
    # Optional extras (requested from Sysgal, never required).
    tribunal_codigo: Optional[str] = None
    estado: Optional[str] = None
    competencia: Optional[str] = None
    eliminada: bool = False
    responsable_desde: Optional[date] = None


@dataclass
class SysgalUsuarioDTO:
    sysgal_id: int
    nombre: str
    perfil: Optional[str] = None
    estado: Optional[str] = None
    correo: Optional[str] = None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> Optional[datetime]:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def _to_date(value: Any) -> Optional[date]:
    parsed = _to_datetime(value)
    return parsed.date() if parsed else None


def map_causa(raw: dict) -> SysgalCausaDTO:
    """JSON item -> ``SysgalCausaDTO``. THE place to edit when Sysgal fixes field names."""
    sysgal_id = _to_int(raw.get("id_causa"))
    if sysgal_id is None:
        raise SysgalError("Sysgal causa item has no usable 'id_causa'")
    # Real shape (2026-11-09): abogado = {id, nombre} (the latest assigned; may be null) and
    # activa = bool. The flat abogado_id / abogado_nombre / estado stay as a fallback.
    abogado_raw = raw.get("abogado")
    abogado: dict = abogado_raw if isinstance(abogado_raw, dict) else {}
    activa = raw.get("activa")
    estado = ("activa" if activa else "terminada") if isinstance(activa, bool) else _text(raw.get("estado"))
    return SysgalCausaDTO(
        sysgal_id=sysgal_id,
        rol=_text(raw.get("rol")) or "",
        tribunal=_text(raw.get("tribunal")) or "",
        rut_cliente=_text(raw.get("rut_cliente")),
        abogado_sysgal_id=_to_int(abogado.get("id", raw.get("abogado_id"))),
        abogado_nombre=_text(abogado.get("nombre", raw.get("abogado_nombre"))),
        etapa=_text(raw.get("etapa")),
        tramite=_text(raw.get("tramite")),
        updated_at=_to_datetime(raw.get("fecha_actualizacion")),
        tribunal_codigo=_text(raw.get("tribunal_codigo")),
        estado=estado,
        competencia=_text(raw.get("competencia")),
        eliminada=bool(raw.get("eliminada")),
        responsable_desde=_to_date(raw.get("responsable_desde")),
    )


def map_usuario(raw: dict) -> SysgalUsuarioDTO:
    """JSON item -> ``SysgalUsuarioDTO``. THE place to edit when Sysgal fixes field names."""
    sysgal_id = _to_int(raw.get("id"))
    if sysgal_id is None:
        raise SysgalError("Sysgal usuario item has no usable 'id'")
    # Real shape (2026-11-09): id, nombre, tipo, tipo_nombre, activo (bool); no personal data.
    activo = raw.get("activo")
    estado = ("activo" if activo else "inactivo") if isinstance(activo, bool) else _text(raw.get("estado"))
    return SysgalUsuarioDTO(
        sysgal_id=sysgal_id,
        nombre=_text(raw.get("nombre")) or "",
        perfil=_text(raw.get("tipo_nombre", raw.get("perfil"))),
        estado=estado,
        correo=_text(raw.get("correo")),
    )


def _extract_items(body: Any) -> list:
    """The item list of a response: ``data`` (or ``items``), or a bare list."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    raise SysgalError("Sysgal body has no item list (HTTP 200)")


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

    def causas(
        self,
        desde_id: int = 0,
        limite: int = MAX_CAUSAS_PER_PAGE,
        actualizado_desde: "datetime | str | None" = None,
        solo_activas: Optional[bool] = None,
    ) -> dict:
        """One page of ``GET /api_sync/causas``: ``{items, siguiente_desde_id}``."""
        if not 1 <= limite <= MAX_CAUSAS_PER_PAGE:
            raise ValueError(f"limite must be between 1 and {MAX_CAUSAS_PER_PAGE}, got {limite}")
        params: dict = {"desde_id": desde_id, "limite": limite}
        if actualizado_desde is not None:
            params["actualizado_desde"] = (
                actualizado_desde.strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(actualizado_desde, datetime)
                else str(actualizado_desde)
            )
        if solo_activas is not None:
            params["solo_activas"] = "1" if solo_activas else "0"  # Sysgal accepts 0 or 1

        body = self._get_json(_CAUSAS_PATH, params)
        items = [map_causa(raw) for raw in _extract_items(body) if isinstance(raw, dict)]
        siguiente = _to_int(body.get("siguiente_desde_id")) if isinstance(body, dict) else None
        return {"items": items, "siguiente_desde_id": siguiente}

    def iter_causas(
        self,
        desde_id: int = 0,
        limite: int = MAX_CAUSAS_PER_PAGE,
        actualizado_desde: "datetime | str | None" = None,
        solo_activas: Optional[bool] = None,
    ) -> Iterator[SysgalCausaDTO]:
        """Yield every causa, following ``siguiente_desde_id`` until null/absent.

        Raises ``SysgalError`` when the cursor does not advance or after
        ``MAX_CAUSAS_PAGES`` pages (a cursor that never ends).
        """
        cursor = desde_id
        for _ in range(MAX_CAUSAS_PAGES):
            page = self.causas(cursor, limite, actualizado_desde, solo_activas)
            yield from page["items"]
            nxt = page["siguiente_desde_id"]
            if nxt is None:
                return
            if nxt <= cursor:
                raise SysgalError("Sysgal causas cursor did not advance")
            cursor = nxt
        raise SysgalError(f"Sysgal causas exceeded {MAX_CAUSAS_PAGES} pages")

    def usuarios(self) -> list[SysgalUsuarioDTO]:
        """``GET /api_sync/usuarios`` mapped to DTOs."""
        body = self._get_json(_USUARIOS_PATH, {})
        return [map_usuario(raw) for raw in _extract_items(body) if isinstance(raw, dict)]

    def estadisticas_cartera(self, desde: date, hasta: date) -> dict:
        """``GET /api_sync/estadisticas_cartera``: Sysgal's cartera/effectiveness stats as received.

        Never recomputed here; the stats dict is returned (and snapshotted) exactly as Sysgal sent
        it. Production wraps it as ``{"success": true, "data": {...}}``; the bare form is accepted too.
        """
        body = self._get_json(
            _ESTADISTICAS_CARTERA_PATH, {"desde": desde.isoformat(), "hasta": hasta.isoformat()}
        )
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            body = body["data"]
        if not isinstance(body, dict) or not isinstance(body.get("por_abogado"), list):
            raise SysgalError("Sysgal estadisticas_cartera body has no 'por_abogado' list (HTTP 200)")
        return body

    def causas_sin_gestion(
        self,
        desde: date,
        hasta: date,
        abogado_id: Optional[int] = None,
        desde_id: Optional[int] = None,
        limite: int = MAX_SIN_GESTION_PER_PAGE,
    ) -> dict:
        """One page of ``GET /api_sync/causas_sin_gestion``, as received (``data`` + ``siguiente_desde_id``)."""
        if not 1 <= limite <= MAX_SIN_GESTION_PER_PAGE:
            raise ValueError(f"limite must be between 1 and {MAX_SIN_GESTION_PER_PAGE}, got {limite}")
        params: dict = {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "limite": limite}
        if abogado_id is not None:
            params["abogado_id"] = abogado_id
        if desde_id is not None:
            params["desde_id"] = desde_id
        body = self._get_json(_SIN_GESTION_PATH, params)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise SysgalError("Sysgal causas_sin_gestion body has no 'data' list (HTTP 200)")
        return body

    def escritos_estadisticas(self, desde: date, hasta: date) -> dict:
        """``GET /api_sync/escritos_estadisticas``: escritos generated in GEDOC, as received.

        Production wraps it as ``{"success": true, "data": {...}}``; the bare form is accepted too.
        """
        body = self._get_json(
            _ESCRITOS_ESTADISTICAS_PATH, {"desde": desde.isoformat(), "hasta": hasta.isoformat()}
        )
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            body = body["data"]
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("por_abogado"), list)
            or not isinstance(body.get("por_tipo"), list)
        ):
            raise SysgalError("Sysgal escritos_estadisticas body has no 'por_abogado'/'por_tipo' lists (HTTP 200)")
        return body

    def escritos(
        self,
        desde: date,
        hasta: date,
        abogado_id: Optional[int] = None,
        tipo_id: Optional[str] = None,
        desde_id: Optional[int] = None,
        limite: int = MAX_ESCRITOS_PER_PAGE,
    ) -> dict:
        """One page of ``GET /api_sync/escritos``, as received (``data`` + ``siguiente_desde_id``)."""
        if not 1 <= limite <= MAX_ESCRITOS_PER_PAGE:
            raise ValueError(f"limite must be between 1 and {MAX_ESCRITOS_PER_PAGE}, got {limite}")
        params: dict = {"desde": desde.isoformat(), "hasta": hasta.isoformat(), "limite": limite}
        if abogado_id is not None:
            params["abogado_id"] = abogado_id
        if tipo_id is not None:
            params["tipo_id"] = tipo_id
        if desde_id is not None:
            params["desde_id"] = desde_id
        body = self._get_json(_ESCRITOS_PATH, params)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise SysgalError("Sysgal escritos body has no 'data' list (HTTP 200)")
        return body

    def _get_json(self, path: str, params: dict) -> Any:
        """GET with the same retry-once / error policy as ``estado_por_ruts``.

        Errors carry only the path and status code — never the key or a body.
        """
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}

        last_error: Optional[str] = None
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as http:
                    response = http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"transport error ({type(exc).__name__})"
                logger.warning("Sysgal GET %s failed (attempt %d/2): %s", path, attempt, last_error)
                continue

            if 500 <= response.status_code < 600:
                last_error = f"HTTP {response.status_code}"
                logger.warning("Sysgal GET %s answered %s (attempt %d/2)", path, last_error, attempt)
                continue

            if response.status_code != 200:
                raise SysgalError(f"Sysgal GET {path} answered HTTP {response.status_code}")

            try:
                return response.json()
            except ValueError:
                # Seen in production (2026-11-09): an intermittent PHP warning printed before the
                # JSON. Asking again returned clean JSON, so retry like a 5xx. The body is never
                # logged (it can carry client data).
                last_error = "non-JSON body (HTTP 200)"
                logger.warning("Sysgal GET %s returned %s (attempt %d/2)", path, last_error, attempt)
                continue

        raise SysgalError(f"Sysgal GET {path} unavailable after retry ({last_error})")

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
