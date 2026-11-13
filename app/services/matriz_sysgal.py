"""Sysgal inputs for the matriz classifier (bulk preload) and targeted reclassification.

The classifier stays pure: ``load_sysgal_inputs`` reads, in ONE query, the data of the
Sysgal causa that decides each linked case, and ``MatrizMappingCache`` carries it.
``reclassify_cases`` re-runs the classifier for a given set of cases (used after the daily
Sysgal causas sync for the cases whose Sysgal etapa/tramite/matriz changed).
"""
import logging
from datetime import datetime
from typing import Iterable, NamedTuple, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.sysgal_causa import SysgalCausa

logger = logging.getLogger(__name__)

ACTIVA = "activa"  # SysgalCausa.estado of a live causa (compared lowercase/trimmed)
MATRICES = ("M1 Baja", "M1 Alta", "M2", "M3")

_CHUNK = 5000


class SysgalInput(NamedTuple):
    """What the classifier reads from the Sysgal causa linked to a case."""

    matriz: Optional[str]
    etapa: Optional[str]
    tramite: Optional[str]


def load_sysgal_inputs(db: Session) -> dict[int, SysgalInput]:
    """``{case_id: SysgalInput}`` for cases linked to an active, non-eliminada Sysgal causa.

    Several rows for one case: the most recently updated wins (ties: highest ``id``).
    One query, no per-case round-trips.
    """
    rows = (
        db.query(SysgalCausa.case_id, SysgalCausa.matriz, SysgalCausa.etapa, SysgalCausa.tramite)
        .filter(
            SysgalCausa.case_id.isnot(None),
            SysgalCausa.eliminada.is_(False),
            func.lower(func.trim(SysgalCausa.estado)) == ACTIVA,
        )
        .order_by(func.coalesce(SysgalCausa.sysgal_updated_at, datetime.min), SysgalCausa.id)
        .all()
    )
    return {case_id: SysgalInput(matriz, etapa, tramite) for case_id, matriz, etapa, tramite in rows}


def reclassify_cases(db: Session, case_ids: Iterable[int]) -> int:
    """Recompute and persist the matriz of the given civil cases (and only those).

    Commits once at the end; returns how many cases were rewritten.
    """
    from app.services.matriz_classifier import MatrizMappingCache, classify_case

    ids = sorted(set(case_ids))
    if not ids:
        return 0
    cache = MatrizMappingCache.load(db)
    cache.preload_latest_movements(db, ids)
    done = 0
    for start in range(0, len(ids), _CHUNK):
        cases = (
            db.query(Case)
            .filter(Case.id.in_(ids[start : start + _CHUNK]), Case.competencia == "civil")
            .all()
        )
        for case in cases:
            result = classify_case(db, case, mapping_cache=cache)
            case.matriz = result.matriz
            case.matriz_etapa = result.matriz_etapa
            case.matriz_origen = result.origen
            case.matriz_computed_at = datetime.utcnow()
            done += 1
    db.commit()
    return done
