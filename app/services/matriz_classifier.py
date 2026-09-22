"""Matriz de clasificación classifier (M1 Baja / M1 Alta / M2 / M3).

Derives Grupo Segal's operating "matriz" for a causa from the PJUD data we
already scrape, instead of relying on the manual entry maintained today in
Sysgal. The matriz decides which lawyer level works the causa and is the
base for tramitación KPIs.

ADVISORY: crash-proof, mirrors ``app.services.procedural_classifier``. Any
exception is caught and turned into an indeterminate result (``matriz=None,
origen="error"``) so a bad row or a scraping gap can never break a sync.

Precedence (first match wins)
------------------------------
1. ``cases.procedure`` signals M3: if it contains "Apremio" or "Tercer"
   (tercerías), the apremio cuaderno is the live one even when the last
   scraped movement's stage is older — the case is ``M3`` / etapa
   ``APREMIO`` / origen ``pjud_procedimiento``.
2. Last movement (most recent by ``movement_date`` desc, ``id`` desc, with a
   non-blank ``stage``): resolve ``matriz_pjud_mapeo`` -> matriz etapa, then
   ``matriz_clasificacion`` for the causa's proc_simple -> matriz. If a
   ``matriz_tramite_override`` row matches (proc_antiguo, matriz_etapa, the
   movement's ``procedure`` i.e. PJUD's "Trámite" field), its matriz wins
   over the etapa-level one. Origen ``pjud_etapa``.
3. No movements at all: ``M1 Baja`` / etapa ``ASIGNACIÓN / PENDIENTE DE
   NOTIFICACIÓN`` / origen ``sin_detalle``. This is a PROVISIONAL default —
   today's detail-scraping backlog (~52.6% of causas) means "no movements"
   often just means "not scraped yet", not "genuinely brand new". ``origen``
   exists precisely so reporting never conflates this with a confirmed M1
   Baja.
4. Unmapped stage (no active ``matriz_pjud_mapeo`` row, or the resolved
   matriz etapa has no ``matriz_clasificacion`` row for this proc_simple):
   matriz ``None``, origen ``no_mapeada``, ``detalle`` records what was
   missing so the gap is reportable via ``GET /matriz/distribucion``.

Procedimiento simple fallback
------------------------------
The business product ("Juicio Ejecutivo Completo", "Vive Tranquilo", ...)
lives only in Sysgal today; ``Case.matriz_proc_simple`` is NULL until that
integration exists. While NULL, classification falls back to the
"Juicio Ejecutivo Completo" row set: 5 of the 6 juicio-ejecutivo products
share that identical etapa->matriz map; only "Accion de Prescripcion"
differs (2 etapas: INGRESO EXCEPCIONES DE PRESCRIPCIÓN and INGRESO ABANDONO
DE PROCEDIMIENTO 3 AÑOS resolve to M1 Baja there vs M1 Alta in the default
map) — so this fallback slightly over-classifies those two etapas for
"Accion de Prescripcion" causas until Sysgal sends the real proc_simple.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import MatrizPjudMapeo
from app.models.matriz_tramite_override import MatrizTramiteOverride
from app.models.movement import Movement

logger = logging.getLogger(__name__)

DEFAULT_PROC_SIMPLE = "Juicio Ejecutivo Completo"

ORIGEN_PROCEDIMIENTO = "pjud_procedimiento"
ORIGEN_ETAPA = "pjud_etapa"
ORIGEN_SIN_DETALLE = "sin_detalle"
ORIGEN_NO_MAPEADA = "no_mapeada"
ORIGEN_ERROR = "error"

_APREMIO_PROCEDURE_SIGNALS = ("apremio", "tercer")
_SIN_DETALLE_ETAPA = "ASIGNACIÓN / PENDIENTE DE NOTIFICACIÓN"
_SIN_DETALLE_MATRIZ = "M1 Baja"


class MatrizResult(NamedTuple):
    """Result of classifying one causa."""

    matriz: Optional[str]
    matriz_etapa: Optional[str]
    origen: str
    detalle: Optional[str] = None


class _ClasificacionEntry(NamedTuple):
    matriz: Optional[str]
    proc_antiguo: str


@dataclass
class MatrizMappingCache:
    """Preloaded matriz reference data, built once per sync/backfill run.

    Holding plain-data (not ORM instances) sidesteps SQLAlchemy's
    expire-on-commit: callers in ``sync_service``/``ingest_service`` commit
    once per case inside a loop, which would otherwise force a fresh DB hit
    per cached row on the very next access.
    """

    pjud_mapeo: dict[str, str] = field(default_factory=dict)
    clasificacion: dict[tuple[str, str], _ClasificacionEntry] = field(default_factory=dict)
    tramite_overrides: dict[tuple[str, str, str], str] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Session) -> "MatrizMappingCache":
        pjud_mapeo = {
            row.pjud_stage: row.matriz_etapa
            for row in db.query(MatrizPjudMapeo).filter(MatrizPjudMapeo.activo.is_(True)).all()
        }
        clasificacion = {
            (row.proc_simple, row.etapa): _ClasificacionEntry(
                matriz=row.matriz, proc_antiguo=row.proc_antiguo
            )
            for row in db.query(MatrizClasificacion).all()
        }
        tramite_overrides = {
            (row.proc_antiguo, row.etapa, row.nombre_tramite): row.matriz
            for row in db.query(MatrizTramiteOverride).all()
        }
        return cls(
            pjud_mapeo=pjud_mapeo,
            clasificacion=clasificacion,
            tramite_overrides=tramite_overrides,
        )


def classify_case(
    db: Session, case: Case, *, mapping_cache: MatrizMappingCache
) -> MatrizResult:
    """Classify *case* into a matriz. Never raises — see module docstring."""
    try:
        return _classify_case(db, case, mapping_cache=mapping_cache)
    except Exception:
        logger.exception(
            "matriz_classifier.classify_case failed for case_id=%s — returning indeterminate",
            getattr(case, "id", "?"),
        )
        return MatrizResult(matriz=None, matriz_etapa=None, origen=ORIGEN_ERROR)


def _classify_case(
    db: Session, case: Case, *, mapping_cache: MatrizMappingCache
) -> MatrizResult:
    procedure = (case.procedure or "").lower()
    if any(signal in procedure for signal in _APREMIO_PROCEDURE_SIGNALS):
        return MatrizResult(matriz="M3", matriz_etapa="APREMIO", origen=ORIGEN_PROCEDIMIENTO)

    last_movement = (
        db.query(Movement)
        .filter(
            Movement.case_id == case.id,
            Movement.stage.isnot(None),
            Movement.stage != "",
        )
        .order_by(Movement.movement_date.desc(), Movement.id.desc())
        .first()
    )
    if last_movement is None:
        return MatrizResult(
            matriz=_SIN_DETALLE_MATRIZ, matriz_etapa=_SIN_DETALLE_ETAPA, origen=ORIGEN_SIN_DETALLE
        )

    stage = last_movement.stage
    matriz_etapa = mapping_cache.pjud_mapeo.get(stage)
    if matriz_etapa is None:
        return MatrizResult(
            matriz=None, matriz_etapa=None, origen=ORIGEN_NO_MAPEADA, detalle=stage
        )

    proc_simple = case.matriz_proc_simple or DEFAULT_PROC_SIMPLE
    entry = mapping_cache.clasificacion.get((proc_simple, matriz_etapa))
    if entry is None:
        return MatrizResult(
            matriz=None,
            matriz_etapa=matriz_etapa,
            origen=ORIGEN_NO_MAPEADA,
            detalle=f"{stage} -> {matriz_etapa} (sin fila en clasificación para proc_simple={proc_simple!r})",
        )

    matriz = entry.matriz
    override_key = (entry.proc_antiguo, matriz_etapa, last_movement.procedure or "")
    override_matriz = mapping_cache.tramite_overrides.get(override_key)
    if override_matriz:
        matriz = override_matriz

    return MatrizResult(matriz=matriz, matriz_etapa=matriz_etapa, origen=ORIGEN_ETAPA)
