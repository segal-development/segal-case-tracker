"""Sysgal causas sync: bring each case's responsible lawyer from Sysgal into our tables.

Read-only toward Sysgal; writes only ``sysgal_usuarios`` / ``sysgal_causas``. It never
changes case ownership (``sysgal_owner_by_case`` is the pure input for a later unit).
Not scheduled: the caller decides when to run it.

PRIVACY: nothing here logs a Sysgal payload; only counts reach the logs.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session, aliased

from app.models.case import Case
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services.sysgal_client import SysgalClient
from app.services.sysgal_matching import AMBIGUA, CALZADA, match_causa, normalize_rol

logger = logging.getLogger(__name__)

ACTIVA = "activa"  # SysgalCausa.estado that may decide an owner (compared lowercase/trimmed)

_CAUSA_FIELDS = (
    "rol", "tribunal", "tribunal_codigo", "rut_cliente", "abogado_sysgal_id", "abogado_nombre",
    "etapa", "tramite", "estado", "competencia", "responsable_desde",
)


def sync_sysgal_causas(db: Session, client: SysgalClient, full: bool = False) -> dict:
    """Upsert usuarios and causas from Sysgal and (re)match every fetched causa.

    Incremental by default: asks Sysgal only for causas updated since the newest
    ``sysgal_updated_at`` we hold; ``full=True`` (or an empty table) fetches everything.
    The human ``lawyer_id`` mapping of a usuario is never touched.
    """
    now = datetime.utcnow()
    stats = {"usuarios": 0, "causas": 0, "nuevas": 0, "actualizadas": 0,
             "calzadas": 0, "sin_calce": 0, "ambiguas": 0}

    existing_users = {u.sysgal_id: u for u in db.query(SysgalUsuario).all()}
    for dto in client.usuarios():
        row = existing_users.get(dto.sysgal_id)
        if row is None:
            row = SysgalUsuario(sysgal_id=dto.sysgal_id)
            db.add(row)
            existing_users[dto.sysgal_id] = row
        row.nombre, row.perfil, row.estado, row.correo = dto.nombre, dto.perfil, dto.estado, dto.correo
        row.synced_at = now
        stats["usuarios"] += 1

    since: Optional[datetime] = None if full else db.query(func.max(SysgalCausa.sysgal_updated_at)).scalar()
    kwargs = {"actualizado_desde": since} if since else {}

    for dto in client.iter_causas(**kwargs):
        row = db.query(SysgalCausa).filter_by(sysgal_id=dto.sysgal_id).one_or_none()
        if row is None:
            row = SysgalCausa(sysgal_id=dto.sysgal_id)
            db.add(row)
            stats["nuevas"] += 1
        else:
            stats["actualizadas"] += 1
        for field in _CAUSA_FIELDS:
            setattr(row, field, getattr(dto, field))
        row.eliminada = bool(dto.eliminada)
        row.sysgal_updated_at = dto.updated_at
        row.rol_norm = normalize_rol(dto.rol)
        row.synced_at = now

        result = match_causa(db, row.rol_norm, dto.tribunal, dto.tribunal_codigo, dto.rut_cliente)
        # Ambiguity is recorded in match_method (case_id stays NULL) so the report can count it.
        row.case_id = result.case_id
        row.match_method = AMBIGUA if result.status == AMBIGUA else result.method
        stats["causas"] += 1
        if result.status == CALZADA:
            stats["calzadas"] += 1
        elif result.status == AMBIGUA:
            stats["ambiguas"] += 1
        else:
            stats["sin_calce"] += 1
        db.flush()

    db.commit()
    logger.info("Sysgal causas sync done: %s", stats)
    return stats


def sysgal_owner_by_case(db: Session) -> dict[int, int]:
    """``{case_id: lawyer_id}`` for matched, active, non-eliminada causas whose responsible is mapped.

    Only causas with ``estado = 'activa'`` count: a terminated Sysgal causa never decides an owner.
    The lawyer also applies to the SIBLING cuadernos of a matched case (our cases with the same
    ``rol`` and ``court_id``), unless a sibling has its own matched causa, which keeps its own owner.
    If several causas point at one case, the most recently updated wins (ties: highest
    ``sysgal_id``), so the result is deterministic. Pure read; one query.
    """
    matched, sibling = aliased(Case), aliased(Case)
    rows = (
        db.query(SysgalCausa.case_id, sibling.id, SysgalUsuario.lawyer_id)
        .join(SysgalUsuario, SysgalUsuario.sysgal_id == SysgalCausa.abogado_sysgal_id)
        .join(matched, matched.id == SysgalCausa.case_id)
        .join(sibling, and_(sibling.rol == matched.rol, sibling.court_id == matched.court_id))
        .filter(
            SysgalCausa.case_id.isnot(None),
            SysgalCausa.eliminada.is_(False),
            func.lower(func.trim(SysgalCausa.estado)) == ACTIVA,
            SysgalUsuario.lawyer_id.isnot(None),
        )
        .order_by(func.coalesce(SysgalCausa.sysgal_updated_at, datetime.min), SysgalCausa.sysgal_id)
        .all()
    )
    direct = {case_id: lawyer_id for case_id, _sibling_id, lawyer_id in rows}  # later rows overwrite earlier
    inherited = {sibling_id: lawyer_id for _case_id, sibling_id, lawyer_id in rows}
    return {**inherited, **direct}
