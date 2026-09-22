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
2. The causa's LAST movement (most recent by ``movement_date`` desc, ``id``
   desc — the truly last one, blank stage or not):
   a. If its ``stage`` has an active ``matriz_pjud_mapeo`` row
      (``match_tipo="stage"``, exact match) -> matriz etapa. Origen
      ``pjud_etapa``.
   b. Otherwise (blank stage, or a stage with no active row) — DESCRIPTION
      FALLBACK: the first active ``match_tipo="descripcion"`` row (ordered by
      ``orden`` then ``id``) whose ``pjud_stage`` value is an accent-/case-
      insensitive SUBSTRING of the movement's ``description`` -> matriz
      etapa. Origen ``pjud_descripcion``. This NEVER overrides a good stage
      match — it only runs when (a) found nothing.
   Either way, the resolved matriz etapa is then looked up in
   ``matriz_clasificacion`` for the causa's proc_simple -> matriz. A
   ``matriz_tramite_override`` row matching (proc_antiguo, matriz_etapa, the
   movement's ``procedure`` i.e. PJUD's "Trámite" field) overrides that
   etapa-level matriz.
3. No movements at all: ``M1 Baja`` / etapa ``ASIGNACIÓN / PENDIENTE DE
   NOTIFICACIÓN`` / origen ``sin_detalle``. This is a PROVISIONAL default —
   today's detail-scraping backlog (~52.5% of causas, measured on the live
   DB 2026-09) means "no movements" often just means "not scraped yet", not
   "genuinely brand new". ``origen`` exists precisely so reporting never
   conflates this with a confirmed M1 Baja.
4. Unmapped (stage AND description both fail to resolve a matriz etapa, or
   the resolved etapa has no ``matriz_clasificacion`` row for this
   proc_simple): matriz ``None``, origen ``no_mapeada``, ``detalle`` records
   what was missing so the gap is reportable via ``GET /matriz/distribucion``
   and ``scripts/recalcular_matriz.py --dry-run``.

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

KNOWN LIMITATION — M1 Alta is effectively unreachable from PJUD data alone
----------------------------------------------------------------------------
M1 Alta comes ONLY from the two etapas above (INGRESO EXCEPCIONES DE
PRESCRIPCIÓN, INGRESO ABANDONO DE PROCEDIMIENTO 3 AÑOS). Measured against
all 127,386 scraped movements (2026-09): the word "prescrip" appears in
ZERO movement descriptions, and "abandono" in only 125 — which never
distinguish the 6-month from the 3-year variant. PJUD simply does not carry
this distinction in the data we scrape. We deliberately do NOT invent a
heuristic for it (e.g. guessing from elapsed time) — that would fabricate a
signal PJUD never gave us. M1 Alta remains a fully valid, supported value
end to end (persisted column, `matriz_clasificacion` rows, API responses,
`?matriz=M1 Alta` filter all work) and becomes reachable once
`Case.matriz_proc_simple` is fed by Sysgal AND/OR a lawyer records this
distinction directly in our system — not from PJUD scraping alone.
"""
from __future__ import annotations

import logging
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import (
    MATCH_TIPO_DESCRIPCION,
    MATCH_TIPO_STAGE,
    MatrizPjudMapeo,
)
from app.models.matriz_tramite_override import MatrizTramiteOverride
from app.models.movement import Movement

logger = logging.getLogger(__name__)

#: Max case ids per IN clause when preloading movements in chunks.
_PRELOAD_CHUNK = 5000

DEFAULT_PROC_SIMPLE = "Juicio Ejecutivo Completo"

ORIGEN_PROCEDIMIENTO = "pjud_procedimiento"
ORIGEN_ETAPA = "pjud_etapa"
ORIGEN_DESCRIPCION = "pjud_descripcion"
ORIGEN_SIN_DETALLE = "sin_detalle"
ORIGEN_NO_MAPEADA = "no_mapeada"
ORIGEN_ERROR = "error"

_APREMIO_PROCEDURE_SIGNALS = ("apremio", "tercer")
_SIN_DETALLE_ETAPA = "ASIGNACIÓN / PENDIENTE DE NOTIFICACIÓN"
_SIN_DETALLE_MATRIZ = "M1 Baja"

# How much of a movement's description to keep in a "no_mapeada" detalle /
# unmapped report — enough to be useful, short enough to not bloat storage.
_DETALLE_SNIPPET_LEN = 120


def _normalize_text(value: Optional[str]) -> str:
    """Lowercase + strip accents, for accent-/case-insensitive matching."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_accents.lower()


class MatrizResult(NamedTuple):
    """Result of classifying one causa."""

    matriz: Optional[str]
    matriz_etapa: Optional[str]
    origen: str
    detalle: Optional[str] = None


class _ClasificacionEntry(NamedTuple):
    matriz: Optional[str]
    proc_antiguo: str


class _DescripcionRule(NamedTuple):
    normalized_substring: str
    matriz_etapa: str


@dataclass(frozen=True)
class _LatestMovement:
    """The fields of a case's most recent movement that the classifier reads."""

    stage: Optional[str]
    description: Optional[str]
    procedure: Optional[str]


@dataclass
class MatrizMappingCache:
    """Preloaded matriz reference data, built once per sync/backfill run.

    Holding plain-data (not ORM instances) sidesteps SQLAlchemy's
    expire-on-commit: callers in ``sync_service``/``ingest_service`` commit
    once per case inside a loop, which would otherwise force a fresh DB hit
    per cached row on the very next access.
    """

    pjud_mapeo: dict[str, str] = field(default_factory=dict)
    descripcion_rules: list[_DescripcionRule] = field(default_factory=list)
    clasificacion: dict[tuple[str, str], _ClasificacionEntry] = field(default_factory=dict)
    tramite_overrides: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: Bulk-preloaded most recent movement per case id. Populated by
    #: :meth:`preload_latest_movements`; absence of a preloaded id means the
    #: case genuinely has no movements (not that it was never loaded).
    latest_movement: dict[int, _LatestMovement] = field(default_factory=dict)
    preloaded_case_ids: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, db: Session) -> "MatrizMappingCache":
        active_mapeo = (
            db.query(MatrizPjudMapeo).filter(MatrizPjudMapeo.activo.is_(True)).all()
        )
        pjud_mapeo = {
            row.pjud_stage: row.matriz_etapa
            for row in active_mapeo
            if row.match_tipo == MATCH_TIPO_STAGE
        }
        descripcion_rows = sorted(
            (row for row in active_mapeo if row.match_tipo == MATCH_TIPO_DESCRIPCION),
            key=lambda row: (row.orden, row.id),
        )
        descripcion_rules = [
            _DescripcionRule(
                normalized_substring=_normalize_text(row.pjud_stage),
                matriz_etapa=row.matriz_etapa,
            )
            for row in descripcion_rows
        ]
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
            descripcion_rules=descripcion_rules,
            clasificacion=clasificacion,
            tramite_overrides=tramite_overrides,
        )

    def preload_latest_movements(
        self, db: Session, case_ids: Optional[list[int]] = None
    ) -> None:
        """Load the most recent movement of many cases in one pass.

        Without this, :func:`classify_case` issues one query per case, which
        over a Cloud SQL proxy turns a full-portfolio backfill into tens of
        thousands of round-trips. Pass ``case_ids=None`` to preload every case.
        """
        query = db.query(
            Movement.case_id,
            Movement.stage,
            Movement.description,
            Movement.procedure,
            Movement.movement_date,
            Movement.id,
        )
        id_chunks: list[Optional[list[int]]]
        if case_ids is None:
            id_chunks = [None]
        else:
            if not case_ids:
                return
            id_chunks = [
                case_ids[i : i + _PRELOAD_CHUNK] for i in range(0, len(case_ids), _PRELOAD_CHUNK)
            ]

        best: dict[int, tuple] = {}
        for chunk in id_chunks:
            rows = query if chunk is None else query.filter(Movement.case_id.in_(chunk))
            for case_id, stage, description, procedure, movement_date, mid in rows.all():
                sort_key = (movement_date, mid)
                current = best.get(case_id)
                if current is None or sort_key > current[0]:
                    best[case_id] = (sort_key, _LatestMovement(stage, description, procedure))
            if chunk is not None:
                self.preloaded_case_ids.update(chunk)

        if case_ids is None:
            self.preloaded_case_ids.update(
                case_id for (case_id,) in db.query(Case.id).all()
            )
        self.latest_movement.update({cid: val for cid, (_, val) in best.items()})

    def match_descripcion(self, description: Optional[str]) -> Optional[str]:
        """First active description rule (by orden, then id) whose substring
        is found in *description* — or ``None``."""
        normalized = _normalize_text(description)
        if not normalized:
            return None
        for rule in self.descripcion_rules:
            if rule.normalized_substring and rule.normalized_substring in normalized:
                return rule.matriz_etapa
        return None


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

    # The TRULY most recent movement — blank stage or not. A blank/unmapped
    # stage on the latest movement is common (PJUD doesn't always tag one)
    # and is handled by the description fallback below, never by silently
    # falling back to an OLDER, possibly-stale movement's stage.
    if case.id in mapping_cache.preloaded_case_ids:
        last_movement = mapping_cache.latest_movement.get(case.id)
    else:
        last_movement = (
            db.query(Movement)
            .filter(Movement.case_id == case.id)
            .order_by(Movement.movement_date.desc(), Movement.id.desc())
            .first()
        )
    if last_movement is None:
        return MatrizResult(
            matriz=_SIN_DETALLE_MATRIZ, matriz_etapa=_SIN_DETALLE_ETAPA, origen=ORIGEN_SIN_DETALLE
        )

    stage = last_movement.stage or ""
    matriz_etapa = mapping_cache.pjud_mapeo.get(stage) if stage else None
    if matriz_etapa is not None:
        origen = ORIGEN_ETAPA
    else:
        matriz_etapa = mapping_cache.match_descripcion(last_movement.description)
        if matriz_etapa is None:
            description_snippet = (last_movement.description or "").strip()[:_DETALLE_SNIPPET_LEN]
            detalle = stage or description_snippet or None
            return MatrizResult(matriz=None, matriz_etapa=None, origen=ORIGEN_NO_MAPEADA, detalle=detalle)
        origen = ORIGEN_DESCRIPCION

    proc_simple = case.matriz_proc_simple or DEFAULT_PROC_SIMPLE
    entry = mapping_cache.clasificacion.get((proc_simple, matriz_etapa))
    if entry is None:
        return MatrizResult(
            matriz=None,
            matriz_etapa=matriz_etapa,
            origen=ORIGEN_NO_MAPEADA,
            detalle=f"{stage or '(sin etapa)'} -> {matriz_etapa} (sin fila en clasificación para proc_simple={proc_simple!r})",
        )

    matriz = entry.matriz
    override_key = (entry.proc_antiguo, matriz_etapa, last_movement.procedure or "")
    override_matriz = mapping_cache.tramite_overrides.get(override_key)
    if override_matriz:
        matriz = override_matriz

    return MatrizResult(matriz=matriz, matriz_etapa=matriz_etapa, origen=origen)


def top_unmapped_movements(
    db: Session, case_ids: list[int], *, limit: Optional[int] = None
) -> list[tuple[Optional[str], Optional[str], int]]:
    """(stage, description_snippet, causas) for the truly most-recent
    movement of each of *case_ids*, grouped and counted by descending count.

    Shared by ``GET /matriz/distribucion`` (``sin_mapear``) and
    ``scripts/recalcular_matriz.py --dry-run`` so both report the exact same
    gaps the classifier would hit. When ``stage`` is blank, groups by the
    description snippet instead (that's what a business reviewer needs to
    write a new ``match_tipo="descripcion"`` mapping row for).
    """
    if not case_ids:
        return []
    rows = (
        db.query(Movement.case_id, Movement.stage, Movement.description, Movement.movement_date, Movement.id)
        .filter(Movement.case_id.in_(case_ids))
        .all()
    )
    latest: dict[int, tuple] = {}
    for case_id, stage, description, movement_date, mid in rows:
        key = (movement_date, mid)
        if case_id not in latest or key > latest[case_id][0]:
            latest[case_id] = (key, stage, description)

    counts: Counter = Counter()
    for _, stage, description in latest.values():
        stage_label = stage or None
        desc_label = (description or "").strip()[:_DETALLE_SNIPPET_LEN] or None
        counts[(stage_label, desc_label if not stage_label else None)] += 1

    ranked = [(stage, desc, n) for (stage, desc), n in counts.most_common()]
    return ranked[:limit] if limit else ranked
