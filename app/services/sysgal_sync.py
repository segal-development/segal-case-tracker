"""sync_sysgal_estados — refresh the per-RUT Sysgal cache for demandados.

Scope: every PARTY of every causa — representatives excluded — whatever the
causa's state. The coverage answer gates whether a causa keeps being
detail-scraped, so it cannot be known only for the subset that already reached
abandono/apremio/prescripción, and it cannot be asked only of the demandado:
the litigante roles are inverted on roughly a quarter of the portfolio, so the
client sits on either side. Which party is the client is decided by who Sysgal
recognises, never by the role label.

Re-asking is throttled per RUT by ``SYSGAL_CACHE_TTL_DAYS`` so the wider scope
does not multiply the per-cycle load on Sysgal's API.

PRIVACY: the Sysgal answer carries nombre/email/telefono — none of it is
stored or logged. Only status codes and counts reach the logs.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.services.sysgal_client import MAX_RUTS_PER_REQUEST, SysgalClient
from app.utils.rut import clean_rut, format_rut

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


#: ``participante`` prefixes that mark a REPRESENTATIVE rather than a party:
#: abogado (``AB.``, ``ABG.``) and apoderado (``AP.``). Everything else counts
#: as a party and gets looked up. The asymmetry is deliberate — looking up a
#: perito by mistake costs one slot in a 100-RUT chunk, while filtering the
#: client out costs the coverage answer for that causa entirely, and PJUD adds
#: role spellings we have not seen.
_REPRESENTATIVE_PREFIXES = ("AB.", "ABG.", "AP.")


def parte_ruts_in_scope(
    db: Session, now: Optional[datetime] = None, force: bool = False
) -> list[str]:
    """Distinct canonical party RUTs still needing a Sysgal answer (sorted).

    Scope is every party of every causa, not the demandado of the causas in
    three states this used to cover. Two separate reasons:

    * The coverage answer decides whether a causa keeps being detail-scraped,
      so it has to be known beyond the subset that already reached
      abandono/apremio/prescripción.
    * **The demandado is not reliably the client.** Where the demandado is a
      company the demandante is a natural person 99.2% of the time, and where
      the demandado is a natural person the demandante is a company 99.6% of
      the time — mirror-image populations, so the roles are inverted on about a
      quarter of the causas. Asking only the demandado leaves those unanswered
      forever, because the party recorded there is the creditor. Sysgal does
      not model creditors at all, so such a RUT can never resolve.

    A RUT answered less than ``SYSGAL_CACHE_TTL_DAYS`` ago is left out: the
    widened scope would otherwise re-ask Sysgal for thousands of unchanged
    RUTs on every worker cycle. The first run fetches the backlog; later runs
    only refresh what went stale. ``force`` bypasses that window for an
    explicit human refresh, which must never be a silent no-op.
    """
    rows = (
        db.query(CaseLitigante.rut)
        .filter(
            CaseLitigante.rut != "",
            *[
                ~CaseLitigante.participante.ilike(f"{prefix}%")
                for prefix in _REPRESENTATIVE_PREFIXES
            ],
        )
        .distinct()
        .all()
    )
    ruts = {clean_rut(r) for (r,) in rows if r}
    ruts.discard("")

    if force:
        return sorted(ruts)

    cutoff = (now or datetime.utcnow()) - timedelta(days=settings.SYSGAL_CACHE_TTL_DAYS)
    fresh = {
        rut
        for (rut,) in db.query(ClienteSysgalEstado.rut)
        .filter(ClienteSysgalEstado.synced_at >= cutoff)
        .all()
    }
    return sorted(ruts - fresh)


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
    force: bool = False,
) -> dict:
    """Query Sysgal for every in-scope party RUT and upsert the cache.

    Never raises for Sysgal-side problems: each 100-RUT chunk is safe-failed
    (logged without PII, counted in ``errores``) and the rest continues. An
    unconfigured client returns ``{"skipped": True, …}`` as a no-op.

    ``force`` re-asks every in-scope RUT even if its cached answer is still
    inside ``SYSGAL_CACHE_TTL_DAYS`` — for the admin-triggered refresh.
    """
    if client is None:
        client = SysgalClient(settings.SYSGAL_BASE_URL, settings.SYSGAL_API_KEY)
    if not client.is_configured:
        logger.warning("Sysgal integration not configured (SYSGAL_BASE_URL/SYSGAL_API_KEY) — skipping sync")
        return _empty_summary(skipped=True)

    summary = _empty_summary(skipped=False)
    ruts = parte_ruts_in_scope(db, force=force)
    summary["consultados"] = len(ruts)
    if not ruts:
        return summary

    for start in range(0, len(ruts), MAX_RUTS_PER_REQUEST):
        chunk = ruts[start : start + MAX_RUTS_PER_REQUEST]
        summary["chunks"] += 1
        # Wire format: Sysgal is VERIFIED to accept the dotted form
        # ("14.183.245-K"); the undotted canonical form is unverified. Send
        # dotted whenever the RUT validates, fall back to canonical otherwise,
        # and always key the cache by the canonical form.
        sent_by_canon = {rut: (format_rut(rut) or rut) for rut in chunk}
        try:
            data = client.estado_por_ruts(list(sent_by_canon.values()))
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
            item = data.get(sent_by_canon[rut]) or {"encontrado": False}
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
