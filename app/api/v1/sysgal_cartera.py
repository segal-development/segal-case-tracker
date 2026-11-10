"""Sysgal cartera report (read-only) and the usuario -> lawyer mapping.

Nothing here changes case ownership: the report compares today's resolved owner
(``resolved_owner_by_case``) with what Sysgal says (``sysgal_owner_by_case``).
"""

import unicodedata
from collections import Counter, defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services.lawyer_roster import resolved_owner_by_case
from app.services.sysgal_causas import sysgal_owner_by_case

router = APIRouter()

EXAMPLES_LIMIT = 20


class UsuarioMappingRequest(BaseModel):
    lawyer_id: Optional[int] = None


def _name_key(name: Optional[str]) -> str:
    """Lowercase, accent-folded, whitespace-collapsed name for comparing people."""
    folded = unicodedata.normalize("NFKD", name or "")
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _proposals(lawyers: list[Lawyer]):
    by_email: dict[str, int] = {}
    by_name: dict[str, list[int]] = defaultdict(list)
    for lw in lawyers:
        if lw.email:
            by_email[lw.email.strip().lower()] = lw.id
        by_name[_name_key(lw.name)].append(lw.id)

    def propose(usuario: SysgalUsuario) -> Optional[int]:
        if usuario.correo and usuario.correo.strip().lower() in by_email:
            return by_email[usuario.correo.strip().lower()]
        ids = by_name.get(_name_key(usuario.nombre), [])
        return ids[0] if len(ids) == 1 else None

    return propose


def _estado_bucket(estado: Optional[str]) -> str:
    key = (estado or "").strip().lower()
    return key if key in ("activa", "terminada") else "otros"


def _calce_counts(causas: list[tuple[Optional[int], Optional[str]]]) -> dict:
    calzadas = sum(1 for case_id, _m in causas if case_id is not None)
    ambiguas = sum(1 for case_id, m in causas if case_id is None and m == "ambigua")
    return {
        "total": len(causas),
        "calzadas": calzadas,
        "sin_calce": len(causas) - calzadas - ambiguas,
        "ambiguas": ambiguas,
    }


@router.get("/cartera/reporte")
async def sysgal_cartera_reporte(
    _admin: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    lawyers = db.query(Lawyer).all()
    lawyer_by_id = {lw.id: lw for lw in lawyers}
    propose = _proposals(lawyers)

    usuarios = [
        {
            "sysgal_id": u.sysgal_id,
            "nombre": u.nombre,
            "perfil": u.perfil,
            "estado": u.estado,
            "lawyer_id": u.lawyer_id,
            "lawyer_nombre": lawyer_by_id[u.lawyer_id].name if u.lawyer_id in lawyer_by_id else None,
            "propuesta_lawyer_id": None if u.lawyer_id else propose(u),
        }
        for u in db.query(SysgalUsuario).order_by(SysgalUsuario.sysgal_id).all()
    ]

    causas = db.query(SysgalCausa.case_id, SysgalCausa.match_method, SysgalCausa.estado).all()
    calce = {
        **_calce_counts([(c, m) for c, m, _e in causas]),
        "por_estado": {
            bucket: _calce_counts([(c, m) for c, m, e in causas if _estado_bucket(e) == bucket])
            for bucket in ("activa", "terminada", "otros")
        },
    }

    hoy = Counter(lw.id for lw in resolved_owner_by_case(db).values())
    con_sysgal = Counter(sysgal_owner_by_case(db).values())
    por_abogado = [
        {
            "lawyer_id": lid,
            "nombre": lawyer_by_id[lid].name if lid in lawyer_by_id else None,
            "hoy": hoy[lid],
            "con_sysgal": con_sysgal[lid],
            "diferencia": con_sysgal[lid] - hoy[lid],
        }
        for lid in sorted(set(hoy) | set(con_sysgal))
    ]

    ejemplos = (
        db.query(SysgalCausa)
        .filter(SysgalCausa.case_id.is_(None), SysgalCausa.match_method.is_(None))
        .order_by(SysgalCausa.sysgal_id)
        .limit(EXAMPLES_LIMIT)
        .all()
    )
    ejemplos_sin_calce = [
        {
            "sysgal_id": c.sysgal_id,
            "rol": c.rol,
            "tribunal": c.tribunal,
            "rut_cliente": c.rut_cliente,
            "abogado_nombre": c.abogado_nombre,
        }
        for c in ejemplos
    ]

    return {
        "usuarios": usuarios,
        "calce": calce,
        "por_abogado": por_abogado,
        "ejemplos_sin_calce": ejemplos_sin_calce,
    }


@router.put("/usuarios/{sysgal_id}")
async def set_sysgal_usuario_mapping(
    sysgal_id: int,
    body: UsuarioMappingRequest,
    admin_rut: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    usuario = db.query(SysgalUsuario).filter_by(sysgal_id=sysgal_id).one_or_none()
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuario de Sysgal no encontrado")
    if body.lawyer_id is not None and db.get(Lawyer, body.lawyer_id) is None:
        raise HTTPException(status_code=404, detail="Abogado no encontrado")

    usuario.lawyer_id = body.lawyer_id
    usuario.mapped_by = admin_rut if body.lawyer_id is not None else None
    db.commit()
    return {"sysgal_id": usuario.sysgal_id, "lawyer_id": usuario.lawyer_id, "mapped_by": usuario.mapped_by}
