"""Daily PJUD public "Búsqueda por Fecha": keep new C causas whose demandado has no lawyer.

The driver (``PublicoFechaDriver`` or a fake) does all the browsing; this module owns the
rules (S3 C-only, S4/L8 no defendant lawyer, S5 what is stored, S7 daily cap) and the
per-(fecha, corte) run records. The demand PDF is read in memory and never stored.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.busqueda_fecha_run import BusquedaFechaRun
from app.models.court import Court
from app.models.demanda_publica import DemandaPublica
from app.scrapper.pjud import publico_fecha as pf
from app.services.pdf_text import extraer_texto_pdf

logger = logging.getLogger(__name__)

_COUNTERS = ("encontradas", "c", "con_abogado", "guardadas", "errores", "detalles")
_LEDGER = "detalles_por_dia"  # {iso day: modal opens made that Chile day by this run row}


def _days(desde: date, hasta: date) -> list[date]:
    return [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]


def _litigantes_json(litigantes: list[pf.Litigante]) -> list[dict]:
    """participante / persona / nombre only — never the RUT."""
    return [{"participante": x.participante, "persona": x.persona, "nombre": x.nombre} for x in litigantes]


def _chile_today() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Santiago")).date()


def detalles_usados_hoy(db: Session, hoy: date) -> int:
    """Modal opens already made on Chile day ``hoy``, summed from the persisted run counters."""
    key = hoy.isoformat()
    total = 0
    for (cont,) in db.query(BusquedaFechaRun.contadores).all():
        total += int(((cont or {}).get(_LEDGER) or {}).get(key, 0))
    return total


def presupuesto_restante(db: Session, hoy: date) -> int:
    return max(0, int(settings.PUBLICO_FECHA_MAX_DETALLES) - detalles_usados_hoy(db, hoy))


def _get_run(db: Session, fecha: date, corte: str) -> BusquedaFechaRun:
    run = db.query(BusquedaFechaRun).filter_by(fecha=fecha, corte=corte).one_or_none()
    if run is None:
        run = BusquedaFechaRun(fecha=fecha, corte=corte, status="running", started_at=datetime.utcnow())
        db.add(run)
    run.status = "running"
    run.started_at = datetime.utcnow()
    run.finished_at = None
    run.contadores = {**dict.fromkeys(_COUNTERS, 0), _LEDGER: dict((run.contadores or {}).get(_LEDGER) or {})}
    return run


def _persist(db, runs, counters, ledger, ledger_base, hoy_key) -> None:
    """Write the running counters so a crash keeps the day's budget accounting."""
    for d, run in runs.items():
        ledger[d][hoy_key] = ledger_base[d] + counters[d]["detalles"]
        run.contadores = {**counters[d], _LEDGER: dict(ledger[d])}
    db.commit()


async def _leer_texto(driver, valor: str | None) -> str | None:
    if not valor:
        return None
    try:
        data = await driver.descargar_demanda(valor)
        return (extraer_texto_pdf(data) or None) if data else None
    except Exception:
        logger.warning("Publico fecha: demand PDF read failed (non-fatal)")
        return None


async def run_busqueda(
    db: Session, driver, desde: date, hasta: date, cortes: list[str], *, hoy: date | None = None
) -> dict:
    """Search ``desde..hasta`` per corte and tribunal; returns a summary of the whole call.

    ``PUBLICO_FECHA_MAX_DETALLES`` bounds the modal opens of the whole Chile day ``hoy``: what
    earlier runs/cortes/restarts already used is read from the persisted run counters.
    """
    hoy = hoy or _chile_today()
    hoy_key = hoy.isoformat()
    cap = presupuesto_restante(db, hoy)
    detalles = 0
    courts = {c.name.strip().lower(): c.id for c in db.query(Court).all()}
    dias = _days(desde, hasta)
    summary = {"guardadas": 0, "errores": 0, "detalles": 0, "tope": False}

    for corte in cortes:
        corte = str(corte).strip()
        label = pf.CORTES.get(corte, corte)
        runs = {d: _get_run(db, d, corte) for d in dias}
        counters = {d: dict.fromkeys(_COUNTERS, 0) for d in dias}  # not on the ORM rows: rollback expires them
        ledger = {d: dict(runs[d].contadores.get(_LEDGER) or {}) for d in dias}
        ledger_base = {d: int(ledger[d].get(hoy_key, 0)) for d in dias}
        pendientes = dict.fromkeys(dias, 0)
        search_failed = False
        _persist(db, runs, counters, ledger, ledger_base, hoy_key)

        try:
            tribunales = await driver.listar_tribunales(corte)
        except Exception:
            logger.exception("Publico fecha: listing tribunals failed (corte=%s)", corte)
            tribunales, search_failed = [], True

        for trib_value, _trib_name in tribunales:
            try:
                filas = await driver.buscar(corte, trib_value, desde, hasta)
            except Exception:
                logger.exception("Publico fecha: search failed (corte=%s tribunal=%s)", corte, trib_value)
                search_failed = True
                continue

            for fila in filas:
                run = runs.get(fila.fecha) if fila.fecha else None
                if run is None:
                    continue
                cnt = counters[fila.fecha]
                cnt["encontradas"] += 1
                if not pf.solo_civiles([fila]):
                    continue
                cnt["c"] += 1
                if (
                    db.query(DemandaPublica.id).filter_by(rol=fila.rol, tribunal=fila.tribunal).first()
                    is not None
                ):
                    continue
                if detalles >= cap:
                    pendientes[fila.fecha] += 1
                    summary["tope"] = True
                    continue
                detalles += 1
                cnt["detalles"] += 1
                try:
                    html = await driver.abrir_detalle(fila)
                    litigantes = pf.parse_litigantes(html)
                    if not pf.sin_abogado_demandado(litigantes):
                        cnt["con_abogado"] += 1
                        continue
                    modal = pf.parse_modal(html)
                    texto = await _leer_texto(driver, pf.extraer_valor_demanda(html))
                    db.add(
                        DemandaPublica(
                            rol=fila.rol,
                            tribunal=fila.tribunal,
                            court_id=courts.get(fila.tribunal.strip().lower()),
                            corte=label,
                            caratulado=modal.caratulado or fila.caratulado,
                            fecha_ingreso=fila.fecha,
                            texto_demanda=texto,
                            litigantes=_litigantes_json(litigantes),
                            encontrada_at=datetime.utcnow(),
                        )
                    )
                    db.flush()
                    cnt["guardadas"] += 1
                except IntegrityError:
                    db.rollback()  # stored concurrently: same causa, never twice
                except Exception:
                    db.rollback()
                    cnt["errores"] += 1
                    logger.exception("Publico fecha: row failed (%s)", fila.rol)
                db.commit()

            _persist(db, runs, counters, ledger, ledger_base, hoy_key)

        now = datetime.utcnow()
        for d, run in runs.items():
            cnt = counters[d]
            if search_failed:
                run.status = "failed" if cnt["guardadas"] == 0 else "partial"
            elif pendientes[d] or cnt["errores"]:
                run.status = "partial"
            else:
                run.status = "done"
            ledger[d][hoy_key] = ledger_base[d] + cnt["detalles"]
            run.contadores = {**cnt, _LEDGER: ledger[d]}
            run.finished_at = now
            summary["guardadas"] += cnt["guardadas"]
            summary["errores"] += cnt["errores"]
        db.commit()

    summary["detalles"] = detalles
    return summary
