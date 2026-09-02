"""sync_sysgal_estados — refresh the per-RUT Sysgal cache for demandados.

Scope: DDO litigantes (``participante ILIKE 'DDO%'`` — ``DDO.``/``DDOR.``;
this prefix excludes ``AB.DDO``/``AP.DDO``, the demandado's lawyers) of causas
in any of the 3 states: abandono disponible, en apremio, prescripción cumplida.

PRIVACY: the Sysgal answer carries nombre/email/telefono — none of it is
stored or logged. Only status codes and counts reach the logs.
"""

import logging
from datetime import date, datetime
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.services.sysgal_client import MAX_RUTS_PER_REQUEST, SysgalClient
from app.utils.rut import clean_rut

logger = logging.getLogger(__name__)


def _empty_summary(skipped: bool) -> dict:
    return {
        "skipped": skipped,
        "consultados": 0,
        "encontrados": 0,
        "no_encontrados": 0,
        "errores": 0,
        "chunks": 0,
    }


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime(value) -> Optional[datetime]:
    """Sysgal timestamps look like ``YYYY-MM-DD HH:MM:SS[.ffffff]``."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def demandado_ruts_in_scope(db: Session) -> list[str]:
    """Distinct canonical DDO RUTs of causas in the 3 states (sorted)."""
    rows = (
        db.query(CaseLitigante.rut)
        .join(Case, Case.id == CaseLitigante.case_id)
        .filter(
            or_(
                Case.abandono_disponible.is_(True),
                Case.en_apremio.is_(True),
                Case.prescripcion_cumplida.is_(True),
            ),
            CaseLitigante.participante.ilike("DDO%"),
            CaseLitigante.rut != "",
        )
        .distinct()
        .all()
    )
    ruts = {clean_rut(r) for (r,) in rows if r}
    ruts.discard("")
    return sorted(ruts)


def _apply_item(row: ClienteSysgalEstado, item: dict, now: datetime) -> None:
    """Copy the non-PII fields of one Sysgal item onto the cache row."""
    encontrado = bool(item.get("encontrado"))
    row.encontrado = encontrado
    row.synced_at = now
    if not encontrado:
        row.estado_codigo = None
        row.estado_label = None
        row.tiene_contrato = None
        row.vigencia_hasta = None
        row.sysgal_updated_at = None
        return

    contrato = item.get("contrato") or {}
    row.estado_codigo = item.get("estado_comercial_codigo")
    row.estado_label = item.get("estado_comercial")
    tiene = item.get("tiene_contrato")
    row.tiene_contrato = bool(tiene) if tiene is not None else None
    row.vigencia_hasta = _parse_date(contrato.get("vigencia_hasta")) if contrato else None
    row.sysgal_updated_at = _parse_datetime(item.get("updated_at"))


def sync_sysgal_estados(
    db: Session,
    client: Optional[SysgalClient] = None,
    today: Optional[date] = None,
) -> dict:
    """Query Sysgal for every in-scope demandado RUT and upsert the cache.

    Never raises for Sysgal-side problems: each 100-RUT chunk is safe-failed
    (logged without PII, counted in ``errores``) and the rest continues. An
    unconfigured client returns ``{"skipped": True, …}`` as a no-op.
    """
    if client is None:
        client = SysgalClient(settings.SYSGAL_BASE_URL, settings.SYSGAL_API_KEY)
    if not client.is_configured:
        logger.warning("Sysgal integration not configured (SYSGAL_BASE_URL/SYSGAL_API_KEY) — skipping sync")
        return _empty_summary(skipped=True)

    summary = _empty_summary(skipped=False)
    ruts = demandado_ruts_in_scope(db)
    summary["consultados"] = len(ruts)
    if not ruts:
        return summary

    for start in range(0, len(ruts), MAX_RUTS_PER_REQUEST):
        chunk = ruts[start : start + MAX_RUTS_PER_REQUEST]
        summary["chunks"] += 1
        try:
            data = client.estado_por_ruts(chunk)
        except Exception as exc:  # noqa: BLE001 — safe-fail per chunk, no PII in the message
            summary["errores"] += 1
            logger.warning(
                "Sysgal chunk %d failed (%d ruts): %s", summary["chunks"], len(chunk), type(exc).__name__
            )
            continue

        now = datetime.utcnow()
        existing = {
            row.rut: row
            for row in db.query(ClienteSysgalEstado).filter(ClienteSysgalEstado.rut.in_(chunk)).all()
        }
        for rut in chunk:
            # Sysgal keys the answer exactly as sent; a missing key means "no answer".
            item = data.get(rut) or {"encontrado": False}
            row = existing.get(rut)
            if row is None:
                row = ClienteSysgalEstado(rut=rut, encontrado=False, synced_at=now)
                db.add(row)
                existing[rut] = row
            _apply_item(row, item, now)
            if row.encontrado:
                summary["encontrados"] += 1
            else:
                summary["no_encontrados"] += 1
        db.commit()

    logger.info(
        "Sysgal sync: %d ruts, %d found, %d not found, %d chunk error(s)",
        summary["consultados"], summary["encontrados"], summary["no_encontrados"], summary["errores"],
    )
    return summary
