"""Sysgal cobertura derivation — pure business rule, no I/O.

Maps the Sysgal commercial state of a client (the causa's demandado) to the
4-value "cobertura" tag shown on the Causas list.

Business meaning of each value:

* ``activo``   — client has commercial coverage (contract in force).
* ``moroso``   — client is delinquent/inactive (``MOROSO_INACTIVO``).
* ``caducado`` — **"sin cobertura"**: the contract ended, was cancelled,
  never confirmed, never existed, or — the known Sysgal defect — the code
  still says ACTIVO but ``vigencia_hasta`` is already in the past.
* ``sin_dato`` — Sysgal does not know the RUT, or answered something we
  cannot interpret.
"""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

COBERTURAS = ("activo", "moroso", "caducado", "sin_dato")

# Closed enum of ``estado_comercial_codigo`` as documented by Sysgal.
_CODES_ACTIVE = frozenset({"ACTIVO", "POR_VENCER"})
_CODES_MOROSO = frozenset({"MOROSO_INACTIVO"})
_CODES_CADUCADO = frozenset(
    {"TERMINADO", "DESISTIDO", "ANULADO", "SIN_CONTRATO", "SIN_CONFIRMAR"}
)

# Unknown codes are logged ONCE per process, not once per row.
_warned_codes: set[str] = set()


def derive_cobertura(
    estado_codigo: Optional[str],
    vigencia_hasta: Optional[date],
    encontrado: bool,
    today: Optional[date] = None,
) -> str:
    """Derive the cobertura tag from a cached Sysgal state.

    Rules, in order:

    1. Not found in Sysgal, or no code → ``sin_dato``.
    2. ``MOROSO_INACTIVO`` → ``moroso``.
    3. ``ACTIVO`` / ``POR_VENCER`` → ``caducado`` when ``vigencia_hasta`` is
       strictly before ``today`` (stale-ACTIVO catch), else ``activo``.
    4. ``TERMINADO`` / ``DESISTIDO`` / ``ANULADO`` / ``SIN_CONTRATO`` /
       ``SIN_CONFIRMAR`` → ``caducado`` ("sin cobertura" in business terms).
    5. Anything else → ``sin_dato`` (warned once, code only — no PII).
    """
    if not encontrado or estado_codigo is None:
        return "sin_dato"

    code = estado_codigo.strip().upper()

    if code in _CODES_MOROSO:
        return "moroso"

    if code in _CODES_ACTIVE:
        today = today or date.today()
        if vigencia_hasta is not None and vigencia_hasta < today:
            return "caducado"
        return "activo"

    if code in _CODES_CADUCADO:
        return "caducado"

    if code not in _warned_codes:
        _warned_codes.add(code)
        logger.warning("Unknown Sysgal estado_comercial_codigo %r — mapped to sin_dato", code)
    return "sin_dato"
