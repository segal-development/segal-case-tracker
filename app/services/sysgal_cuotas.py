"""Sysgal cuotas: store per-contract mora data and derive "mora bloquea el poder" per case.

Storage (``sysgal_contratos``): one row per contract, upserted by ``contrato``. A contract
missing from a FULL successful sync is DELETED (Sysgal's copy is complete every day, so absence
= no longer reported; a stale moroso row must not keep blocking). A failed sync raises before
any write; ``encontrado=false`` ("no information") also changes nothing. The caller commits.

Safety guards: a sync with ZERO contracts is treated as no information, and a sync that would
delete more than ``MAX_DELETION_RATIO`` (20%) of the stored contracts is refused; both write
nothing (``omitido`` in the stats) and the latter logs only counts.

``copia_at`` = the envelope's ``actualizado`` (Sysgal's daily ~07:00 copy of Contralor data),
falling back to the rows' ``actualizado``. ``actualizacion`` is just a description
(``"diaria (~07:00)"``) and is never used (see ``SysgalClient.cuotas_contratos``).

Derivation (Carla S4): a case is blocked when ANY contract listing one of its (non-deleted)
Sysgal causas is ``contrato_moroso`` AND ``cuota_actual`` in (1, 2). No contract lists any of
its causas -> ``sin_informacion`` (unknown: not blocked, but never reported as al día).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_contrato import SysgalContrato

MORA_BLOQUEA = "bloquea"
MORA_NO_BLOQUEA = "no_bloquea"
MORA_SIN_INFO = "sin_informacion"

CUOTAS_QUE_BLOQUEAN = (1, 2)

# A full sync that would delete more than this share of the stored contracts is refused.
MAX_DELETION_RATIO = 0.20

logger = logging.getLogger(__name__)


class _ContratoLike(Protocol):
    cuota_actual: int | None
    contrato_moroso: bool
    id_causas: list[int]


def sync_sysgal_cuotas(db: Session, client) -> dict:
    """Pull the full cuotas copy and mirror it into ``sysgal_contratos`` (caller commits)."""
    result = client.cuotas_contratos()  # raises on any failure, before touching the DB
    if not result.encontrado:
        return {"encontrado": False, "insertados": 0, "actualizados": 0, "eliminados": 0, "omitido": True}
    if not result.contratos:
        logger.warning("Sysgal cuotas sync returned zero contracts; treated as no information")
        return {"encontrado": True, "insertados": 0, "actualizados": 0, "eliminados": 0, "omitido": True}

    now = datetime.utcnow()
    existing = {r.contrato: r for r in db.query(SysgalContrato).all()}
    seen = {dto.contrato for dto in result.contratos}
    to_delete = len(set(existing) - seen)
    if existing and to_delete / len(existing) > MAX_DELETION_RATIO:
        logger.warning(
            "Sysgal cuotas sync refused: it would delete %d of %d stored contracts (> %d%%)",
            to_delete, len(existing), int(MAX_DELETION_RATIO * 100),
        )
        return {"encontrado": True, "insertados": 0, "actualizados": 0, "eliminados": 0, "omitido": True}
    seen = set()
    inserted = updated = 0
    for dto in result.contratos:
        seen.add(dto.contrato)
        row = existing.get(dto.contrato)
        if row is None:
            row = SysgalContrato(contrato=dto.contrato)
            db.add(row)
            inserted += 1
        else:
            updated += 1
        row.cuota_actual = dto.cuota_actual
        row.contrato_moroso = dto.contrato_moroso
        row.contrato_al_dia = dto.contrato_al_dia
        row.id_causas = list(dto.id_causas)
        row.copia_at = result.copia_at
        row.synced_at = now
    removed = 0
    for contrato, row in existing.items():
        if contrato not in seen:
            db.delete(row)
            removed += 1
    db.flush()
    return {"encontrado": True, "insertados": inserted, "actualizados": updated, "eliminados": removed}


def derive_mora(causas_por_case: dict[int, set[int]], contratos: Iterable[_ContratoLike]) -> dict[int, str]:
    """Pure: ``{case_id: MORA_*}`` from each case's Sysgal causa ids and the contracts."""
    por_causa: dict[int, list[_ContratoLike]] = defaultdict(list)
    for contrato in contratos:
        for causa_id in contrato.id_causas or []:
            por_causa[causa_id].append(contrato)
    out: dict[int, str] = {}
    for case_id, causas in causas_por_case.items():
        linked = [c for causa_id in causas for c in por_causa.get(causa_id, [])]
        if not linked:
            out[case_id] = MORA_SIN_INFO
        elif any(c.contrato_moroso and c.cuota_actual in CUOTAS_QUE_BLOQUEAN for c in linked):
            out[case_id] = MORA_BLOQUEA
        else:
            out[case_id] = MORA_NO_BLOQUEA
    return out


def load_mora_por_case(db: Session, case_ids: list[int]) -> dict[int, str]:
    """Batch ``derive_mora`` for ``case_ids``: two queries regardless of how many cases."""
    if not case_ids:
        return {}
    causas_por_case: dict[int, set[int]] = {cid: set() for cid in case_ids}
    for sysgal_id, case_id in (
        db.query(SysgalCausa.sysgal_id, SysgalCausa.case_id)
        .filter(SysgalCausa.case_id.in_(case_ids), SysgalCausa.eliminada.is_(False))
        .all()
    ):
        causas_por_case[case_id].add(sysgal_id)
    return derive_mora(causas_por_case, db.query(SysgalContrato).all())


def cuotas_actualizadas_al(db: Session) -> datetime | None:
    """Copy timestamp of the stored cuotas (None if never synced)."""
    return db.query(func.max(SysgalContrato.copia_at)).scalar()


# ---- estado de pago per case (S9, display only: blocks nothing) ----
PAGO_MOROSO = "moroso"
PAGO_PENDIENTE = "pendiente"
PAGO_AL_DIA = "al_dia"
PAGO_SIN_INFO = "sin_informacion"

# Worst first: moroso > pendiente > al_dia.
_PAGO_RANK = {PAGO_MOROSO: 0, PAGO_PENDIENTE: 1, PAGO_AL_DIA: 2}


@dataclass(frozen=True)
class EstadoPago:
    estado: str
    cuota_actual: int | None
    #: moroso AND cuota_actual in (1, 2): the case the poder gate would hold.
    bloquearia_poder: bool
    copia_at: datetime | None


def _estado_de_contrato(c: _ContratoLike) -> str:
    if c.contrato_moroso:
        return PAGO_MOROSO
    if getattr(c, "contrato_al_dia", False):
        return PAGO_AL_DIA
    return PAGO_PENDIENTE


def derive_estado_pago(causas_por_case: dict[int, set[int]], contratos: Iterable) -> dict[int, EstadoPago]:
    """Pure: ``{case_id: EstadoPago}`` for cases WITH Sysgal causas.

    Several contracts: the worst estado wins. Ties: a moroso contract that would block
    (cuota 1/2) first, then the lowest cuota_actual, then the contract number (deterministic).
    """
    contratos = list(contratos)
    por_causa: dict[int, list] = defaultdict(list)
    for contrato in contratos:
        for causa_id in contrato.id_causas or []:
            por_causa[causa_id].append(contrato)
    out: dict[int, EstadoPago] = {}
    for case_id, causas in causas_por_case.items():
        if not causas:
            continue
        linked = {c.contrato: c for causa_id in causas for c in por_causa.get(causa_id, [])}
        if not linked:
            out[case_id] = EstadoPago(PAGO_SIN_INFO, None, False, None)
            continue

        def key(c):
            estado = _estado_de_contrato(c)
            bloquea = estado == PAGO_MOROSO and c.cuota_actual in CUOTAS_QUE_BLOQUEAN
            cuota = c.cuota_actual if c.cuota_actual is not None else 10**6
            return (_PAGO_RANK[estado], not bloquea, cuota, c.contrato)

        best = min(linked.values(), key=key)
        estado = _estado_de_contrato(best)
        out[case_id] = EstadoPago(
            estado,
            best.cuota_actual,
            estado == PAGO_MOROSO and best.cuota_actual in CUOTAS_QUE_BLOQUEAN,
            best.copia_at,
        )
    return out


def estado_pago_por_case(db: Session, case_ids: Iterable[int]) -> dict[int, EstadoPago]:
    """Batch ``derive_estado_pago``: two queries for any number of cases.

    Cases without any (non-deleted) Sysgal causa are absent from the result (-> null).
    """
    ids = list(case_ids)
    if not ids:
        return {}
    causas_por_case: dict[int, set[int]] = {}
    for sysgal_id, case_id in (
        db.query(SysgalCausa.sysgal_id, SysgalCausa.case_id)
        .filter(SysgalCausa.case_id.in_(ids), SysgalCausa.eliminada.is_(False))
        .all()
    ):
        causas_por_case.setdefault(case_id, set()).add(sysgal_id)
    if not causas_por_case:
        return {}
    return derive_estado_pago(causas_por_case, db.query(SysgalContrato).all())
