"""Consumer of the Sysgal incorporation queue (stage 2).

Fetches each pending causa from the public PJUD consulta by ROL (through an injected
``fetch(rol, corte)``) and creates the ``Case`` under the lawyer mapped from Sysgal, flagged
``patrocinio_pendiente``. The pace comes from the DB (``app_settings``) and is counted from the
queue rows' own timestamps; a brake stops the run for the rest of the Chile day.

This module never opens a PJUD session itself and never logs credentials or page bodies.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_incorporacion import SysgalIncorporacion
from app.models.sysgal_usuario import SysgalUsuario
from app.models.webhook import Webhook
from app.scrapper.pjud.exceptions import ConsultaSessionExpired, InvalidCredentialsError, LoginPageError
from app.services import app_settings
from app.services.document_persistence import DocumentPersistenceService
from app.services.matriz_classifier import MatrizMappingCache
from app.services.notification_service import NotificationService
from app.services.sync_service import (
    SPEC_ESCRITO,
    SPEC_EXHORTO,
    SPEC_LITIGANTE,
    SPEC_NOTIFICACION,
    NotifyBudget,
    SyncService,
    _maybe_classify_matriz,
    _maybe_recompute_deadlines,
    _sync_entities,
    convert_api_movements_to_scraped,
)
from app.services.telegram_notifier import send_telegram

logger = logging.getLogger(__name__)

CHILE = ZoneInfo("America/Santiago")

PENDIENTE = "pendiente"
INCORPORADA = "incorporada"
NO_VISIBLE = "no_visible"
ERROR = "error"

MATCH_METHOD = "pjud_rol"
MAX_INTENTOS = 3
BACKOFF = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
BRAKE_STREAK = 3

SIN_ABOGADO = "abogado sin relacionar"
KIND_SESSION = "sesión de PJUD expirada"
KIND_EMPTY = "detalle vacío"
KIND_HTTP = "error HTTP de PJUD"
KIND_OTHER = "error inesperado"
KIND_LOGIN = "login rechazado por PJUD"

Fetch = Callable[[str, int], Awaitable[Optional[Any]]]


def _utc_naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def consumo(db: Session, now: datetime) -> dict:
    """PJUD attempts made in the current Chile day and clock hour, from the queue rows."""
    local = now.astimezone(CHILE)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_start = local.replace(minute=0, second=0, microsecond=0)

    def since(start: datetime) -> int:
        return (
            db.query(func.count(SysgalIncorporacion.id))
            .filter(SysgalIncorporacion.ultimo_intento_at >= _utc_naive(start))
            .scalar()
        )

    return {"dia": since(day_start), "hora": since(hour_start)}


def _day_code(now: datetime) -> int:
    return int(now.astimezone(CHILE).strftime("%Y%m%d"))


def _classify(exc: Exception) -> str:
    if isinstance(exc, (InvalidCredentialsError, LoginPageError)):
        return KIND_LOGIN
    if isinstance(exc, ConsultaSessionExpired):
        return KIND_SESSION
    if isinstance(exc, httpx.HTTPError):
        return KIND_HTTP
    return KIND_OTHER


def _mapped_lawyer_id(db: Session, causa: SysgalCausa) -> Optional[int]:
    if causa.abogado_sysgal_id is None:
        return None
    usuario = db.query(SysgalUsuario).filter_by(sysgal_id=causa.abogado_sysgal_id).first()
    return usuario.lawyer_id if usuario is not None else None


def _existing_case(db: Session, rol: str, court_id: Optional[int]) -> Optional[Case]:
    """Same ROL in the same court, any owner, Principal cuaderno (or none)."""
    return (
        db.query(Case)
        .filter(
            Case.rol == rol,
            Case.court_id == court_id,
            func.lower(func.coalesce(Case.procedure, "")).in_(("", "principal")),
        )
        .order_by(Case.id)
        .first()
    )


def _link(q: SysgalIncorporacion, causa: SysgalCausa, case_id: int) -> None:
    q.estado, q.case_id, q.ultimo_error, q.proximo_intento_at = INCORPORADA, case_id, None, None
    causa.case_id, causa.match_method = case_id, MATCH_METHOD


def _persist_found(db: Session, q: SysgalIncorporacion, causa: SysgalCausa, lawyer_id: int, detail) -> int:
    """Create the case, link it and store movements/entities/documents. Returns the case id.

    Case + links are flushed before the first commit (made by ``sync_movements``), so they land
    in the same transaction as the movements; a failure before that point rolls everything back.
    """
    svc = SyncService(db)
    plaintiff, defendant = svc._parse_caratulado(detail.case.caratulado)
    now = datetime.utcnow()
    case = Case(
        lawyer_id=lawyer_id,
        court_id=q.court_id,
        rol=causa.rol_norm,
        plaintiff=plaintiff,
        defendant=defendant,
        status=svc._map_status(detail.case.estado_cuaderno or ""),
        procedure=detail.case.cuaderno or "Principal",
        competencia="civil",
        filed_at=svc._parse_date(detail.case.fecha_ingreso),
        patrocinio_pendiente=True,
        created_at=now,
        updated_at=now,
    )
    db.add(case)
    db.flush()
    _link(q, causa, case.id)
    db.flush()

    webhooks = db.query(Webhook).filter(Webhook.lawyer_id == lawyer_id, Webhook.is_active == True).all()  # noqa: E712
    notification_svc = NotificationService(db)
    budget = NotifyBudget.from_settings()
    try:
        scraped = convert_api_movements_to_scraped([
            {
                "folio": m.folio, "fecha": m.fecha, "tipo_tramite": m.tipo_tramite,
                "descripcion": m.descripcion, "etapa": m.etapa, "foja": m.foja,
                "tiene_documento": m.tiene_documento,
            }
            for m in detail.movements
        ])
        if scraped:
            # Historic batch: stored, never alerted (only later syncs of this case alert).
            svc.sync_movements(case_id=int(case.id), scraped_movements=scraped, budget=budget,
                               notification_svc=notification_svc, silent=True)
        db.commit()  # case + links (+ movements) are now durable

        escrito_rows = None
        for entities, spec in (
            (detail.litigantes, SPEC_LITIGANTE),
            (detail.notificaciones, SPEC_NOTIFICACION),
            (detail.escritos, SPEC_ESCRITO),
            (detail.exhortos, SPEC_EXHORTO),
        ):
            # No case/lawyer context -> _sync_entities stores the rows without alerts.
            synced = _sync_entities(db, int(case.id), entities, spec, webhooks=webhooks,
                                    notification_svc=notification_svc, budget=budget)
            if spec is SPEC_ESCRITO:
                escrito_rows = synced
        DocumentPersistenceService().persist_from_detail(detail, int(case.id), db, escrito_rows=escrito_rows)
        if detail.movements:
            case.last_detail_checked_at = datetime.utcnow()
        _maybe_recompute_deadlines(db, case, budget=budget)
        _maybe_classify_matriz(db, case, mapping_cache=MatrizMappingCache.load(db))
        db.commit()
    finally:
        notification_svc.close()
    return int(case.id)


def _record_failure(db: Session, q_id: int, kind: str, now: datetime) -> None:
    q = db.get(SysgalIncorporacion, q_id)
    q.intentos = (q.intentos or 0) + 1
    q.ultimo_error = kind[:255]
    q.ultimo_intento_at = _utc_naive(now)
    if q.intentos >= MAX_INTENTOS:
        q.estado, q.proximo_intento_at = ERROR, None
    else:
        q.proximo_intento_at = _utc_naive(now) + BACKOFF[q.intentos - 1]
    db.commit()


async def _brake(db: Session, motivo: str, now: datetime, notify: Callable[[str], bool]) -> None:
    app_settings.set_int(db, app_settings.KEY_FRENO_DIA, _day_code(now))
    db.commit()
    logger.error("Sysgal incorporacion braked for the Chile day: %s", motivo)
    await asyncio.to_thread(notify, f"Incorporación desde Sysgal detenida: {motivo}. Se reintenta mañana.")


async def run_incorporacion(
    db: Session,
    fetch: Fetch,
    *,
    notify: Callable[[str], bool] = send_telegram,
    egress_ok: Optional[Callable[[], bool]] = None,
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(CHILE)
    stats = {"estado": "ok", "incorporadas": 0, "vinculadas": 0, "no_visible": 0, "sin_abogado": 0,
             "fallos": 0, "frenada": False}

    ritmo = app_settings.get_ritmo(db)
    if ritmo["por_dia"] <= 0:
        stats["estado"] = "apagada"
        return stats
    if app_settings.get_int(db, app_settings.KEY_FRENO_DIA, 0) == _day_code(now):
        stats["estado"] = "frenada"
        stats["frenada"] = True
        return stats
    used = consumo(db, now)
    cupo = min(ritmo["por_dia"] - used["dia"], ritmo["por_hora"] - used["hora"])
    if cupo <= 0:
        stats["estado"] = "sin_cupo"
        return stats
    def _default_egress() -> bool:
        from app.services.egress_guard import egress_allowed

        return egress_allowed()[0]

    check_egress: Callable[[], bool] = egress_ok if egress_ok is not None else _default_egress

    due = (
        db.query(SysgalIncorporacion.id)
        .filter(
            SysgalIncorporacion.estado == PENDIENTE,
            SysgalIncorporacion.court_id.isnot(None),
            SysgalIncorporacion.corte.isnot(None),
            (SysgalIncorporacion.proximo_intento_at.is_(None))
            | (SysgalIncorporacion.proximo_intento_at <= _utc_naive(now)),
        )
        .order_by(SysgalIncorporacion.prioridad, SysgalIncorporacion.id)
        .all()
    )
    attempts = 0
    streak_kind: Optional[str] = None
    streak = 0
    for (q_id,) in due:
        if attempts >= cupo:
            break
        q = db.get(SysgalIncorporacion, q_id)
        causa = db.get(SysgalCausa, q.sysgal_causa_id)
        rol, court_id, corte = causa.rol_norm, q.court_id, q.corte

        existing = _existing_case(db, rol, court_id)
        if existing is not None:
            _link(q, causa, existing.id)
            db.commit()
            stats["vinculadas"] += 1
            continue
        lawyer_id = _mapped_lawyer_id(db, causa)
        if lawyer_id is None:
            q.estado, q.ultimo_error = ERROR, SIN_ABOGADO
            db.commit()
            stats["sin_abogado"] += 1
            continue
        if not check_egress():
            stats["estado"] = "egress_no_permitido"
            break

        attempts += 1
        try:
            detail = await fetch(rol, corte)
        except Exception as exc:  # noqa: BLE001 — classified; the message is never stored
            kind = _classify(exc)
            logger.warning("Sysgal incorporacion: PJUD failure (%s) for %s", kind, rol)
            db.rollback()
            if kind == KIND_LOGIN:
                stats["frenada"] = True
                await _brake(db, kind, now, notify)
                break
            _record_failure(db, q_id, kind, now)
            stats["fallos"] += 1
            streak = streak + 1 if kind == streak_kind else 1
            streak_kind = kind
            if streak >= BRAKE_STREAK:
                stats["frenada"] = True
                await _brake(db, kind, now, notify)
                break
            continue

        if detail is not None and not detail.movements and not detail.litigantes:
            _record_failure(db, q_id, KIND_EMPTY, now)
            stats["fallos"] += 1
            streak = streak + 1 if KIND_EMPTY == streak_kind else 1
            streak_kind = KIND_EMPTY
            if streak >= BRAKE_STREAK:
                stats["frenada"] = True
                await _brake(db, KIND_EMPTY, now, notify)
                break
            continue

        streak_kind, streak = None, 0
        if detail is None:
            q.estado, q.ultimo_error = NO_VISIBLE, None
            q.ultimo_intento_at = _utc_naive(now)
            db.commit()
            stats["no_visible"] += 1
            continue
        try:
            q.ultimo_intento_at = _utc_naive(now)
            _persist_found(db, q, causa, lawyer_id, detail)
            stats["incorporadas"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Sysgal incorporacion: could not store %s (%s)", rol, type(exc).__name__)
            db.rollback()
            _record_failure(db, q_id, "error al guardar", now)
            stats["fallos"] += 1

    logger.info("Sysgal incorporacion run: %s", stats)
    return stats
