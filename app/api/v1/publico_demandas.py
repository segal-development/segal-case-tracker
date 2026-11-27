"""Public, no-auth read of the causas found without demandado lawyer (secret link).

Mounted directly in ``app.main`` (outside ``api_router``/``procurador_gate``).
The token is a path param named ``token`` (redacted by the activity audit) and is never logged.
"""

import hashlib
import hmac
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.demanda_publica import DemandaPublica
from app.models.public_link_token import PURPOSE_DEMANDAS_PUBLICAS, PublicLinkToken

router = APIRouter()

INVALID_DETAIL = "Link no válido."
_HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"}


def _no_index(response: Response) -> None:
    for k, v in _HEADERS.items():
        response.headers[k] = v


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _invalid() -> HTTPException:
    return HTTPException(status_code=404, detail=INVALID_DETAIL, headers=_HEADERS)


def _validate_token(db: Session, token: str) -> PublicLinkToken:
    digest = hash_token(token)
    row = (
        db.query(PublicLinkToken)
        .filter(
            PublicLinkToken.token_hash == digest,
            PublicLinkToken.purpose == PURPOSE_DEMANDAS_PUBLICAS,
        )
        .first()
    )
    if row is None or row.revoked_at is not None or not hmac.compare_digest(row.token_hash, digest):
        raise _invalid()
    row.last_used_at = datetime.utcnow()
    db.commit()
    return row


@router.get("/demandas/{token}")
def listar_demandas(
    token: str,
    response: Response,
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _no_index(response)
    _validate_token(db, token)
    query = db.query(DemandaPublica)
    if desde is not None:
        query = query.filter(DemandaPublica.fecha_ingreso >= desde)
    if hasta is not None:
        query = query.filter(DemandaPublica.fecha_ingreso <= hasta)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                DemandaPublica.rol.ilike(like),
                DemandaPublica.caratulado.ilike(like),
                DemandaPublica.tribunal.ilike(like),
            )
        )
    total = query.count()
    rows = (
        query.order_by(DemandaPublica.fecha_ingreso.desc(), DemandaPublica.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "items": [
            {
                "id": d.id,
                "rol": d.rol,
                "tribunal": d.tribunal,
                "caratulado": d.caratulado,
                "fecha_ingreso": d.fecha_ingreso,
                "encontrada_at": d.encontrada_at,
                "tiene_texto": bool(d.texto_demanda),
            }
            for d in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/demandas/{token}/{demanda_id}")
def detalle_demanda(token: str, demanda_id: int, response: Response, db: Session = Depends(get_db)):
    _no_index(response)
    _validate_token(db, token)
    d = db.query(DemandaPublica).filter(DemandaPublica.id == demanda_id).first()
    if d is None:
        raise _invalid()
    return {
        "id": d.id,
        "rol": d.rol,
        "tribunal": d.tribunal,
        "caratulado": d.caratulado,
        "fecha_ingreso": d.fecha_ingreso,
        "encontrada_at": d.encontrada_at,
        "texto_demanda": d.texto_demanda,
    }
