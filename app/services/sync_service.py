"""
Sync Service - Synchronize PJUD data with local database.

Responsibilities:
1. Take scraped data from PJUD
2. Compare with existing data in DB
3. Detect new/changed cases and movements
4. Create alerts for changes
5. Track sync history

Performance optimizations:
- Parallel case detail fetching with configurable concurrency limit
"""

import asyncio
import hashlib
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple, Awaitable, TypeVar
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

# Maximum concurrent case detail fetches
MAX_CONCURRENT_FETCHES = 3

T = TypeVar("T")

from app.config import settings
from app.models.case import Case
from app.models.movement import Movement
from app.models.court import Court
from app.models.alert import Alert
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory
from app.models.webhook import Webhook
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.services.notification_service import NotificationService
from app.services.document_persistence import DocumentPersistenceService
from app.scrapper.pjud.exceptions import (
    ConsultaSessionExpired,
    SessionExpiredError,
    SessionNotAuthenticatedError,
    ShapeChallengeError,
)
from app.services.shape_cooldown import get_shape_cooldown
from app.services.deadline_engine import DeadlineEngine, SemaforoTransition


@dataclass
class ScrapedCase:
    """Case data from PJUD scraper."""
    rol: str
    tribunal: str
    caratulado: str
    fecha_ingreso: str
    estado_cuaderno: str
    cuaderno: str
    institucion: Optional[str] = None
    competencia: str = "civil"


@dataclass
class ScrapedMovement:
    """Movement data from PJUD scraper."""
    folio: str
    fecha: str
    tipo_tramite: str
    descripcion: str
    etapa: Optional[str] = None
    foja: Optional[str] = None
    tiene_documento: bool = False


@dataclass
class SyncResult:
    """Result of a sync operation."""
    cases_total: int = 0
    cases_new: int = 0
    cases_updated: int = 0
    movements_new: int = 0
    alerts_created: int = 0
    errors: List[str] = None
    
    def __post_init__(self):
        if self.errors is None:
            self.errors = []
    
    def to_dict(self) -> dict:
        return {
            "cases_total": self.cases_total,
            "cases_new": self.cases_new,
            "cases_updated": self.cases_updated,
            "movements_new": self.movements_new,
            "alerts_created": self.alerts_created,
            "errors": self.errors,
        }


# ============================================================================
# NotifyBudget — shared dispatch counter (ADR-005, Slice 2)
# ============================================================================


class NotifyBudget:
    """Mutable notification dispatch counter shared across all entity types in one sync.

    ADR-005: NOTIFY_MAX_PER_SYNC is a single per-sync budget that applies across
    movements + all entity types combined.  Each entity type drains from this
    shared pool rather than having its own independent cap.  Alerts are ALWAYS
    persisted; only the email/webhook DISPATCH is gated by this counter.
    """

    def __init__(self, limit: int) -> None:
        self._remaining = limit

    @classmethod
    def from_settings(cls) -> "NotifyBudget":
        """Create a budget initialised from settings.NOTIFY_MAX_PER_SYNC."""
        return cls(settings.NOTIFY_MAX_PER_SYNC)

    @property
    def remaining(self) -> int:
        return self._remaining

    def decrement(self) -> None:
        """Consume one dispatch slot (no-op if already exhausted)."""
        if self._remaining > 0:
            self._remaining -= 1

    def exhausted(self) -> bool:
        """True when no dispatch slots remain."""
        return self._remaining <= 0


# ============================================================================
# Case-detail entity persistence helpers (Slice 1b — S1-T10 + S1-T11)
# ============================================================================

# ---------------------------------------------------------------------------
# normalize_cell: single normalisation point used by ALL natural_key helpers
# ---------------------------------------------------------------------------

def normalize_cell(s: str) -> str:
    """Strip, collapse whitespace, and casefold a cell string from PJUD HTML.

    PJUD table cells are frequently padded with multiple spaces and mixed case.
    Normalising before hashing ensures consistent natural_keys across syncs.
    """
    return re.sub(r"\s+", " ", s.strip()).casefold()


# ---------------------------------------------------------------------------
# natural_key functions — one per entity type
# ---------------------------------------------------------------------------

def litigante_natural_key(lit: Any) -> str:
    """Derive a stable 64-char hex key for a PJUDLitigante.

    Key strategy (ADR-002):
    - If RUT is present: sha256(normalize(rut) | normalize(participante))
      — includes participante so the same RUT in different roles is distinct.
    - Fallback (no RUT): sha256(normalize(participante) | normalize(nombre))
    """
    rut = normalize_cell(lit.rut)
    participante = normalize_cell(lit.participante)
    nombre = normalize_cell(lit.nombre)

    if rut:
        raw = f"{rut}|{participante}"
    else:
        raw = f"{participante}|{nombre}"

    return hashlib.sha256(raw.encode()).hexdigest()


def exhorto_natural_key(exh: Any) -> str:
    """Derive a stable 64-char hex key for a PJUDExhorto.

    Key: sha256(normalize(rol_origen) | normalize(rol_destino) | normalize(tipo_exhorto))
    """
    raw = "|".join([
        normalize_cell(exh.rol_origen),
        normalize_cell(exh.rol_destino),
        normalize_cell(exh.tipo_exhorto),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def notificacion_natural_key(notif: Any) -> str:
    """Derive a stable 64-char hex key for a PJUDNotificacion.

    Volatile fields (estado_notif, obs_fallida) are excluded from the key so
    that a state change updates the existing row instead of inserting a new one.
    Key: sha256(rol | tipo_notif | fecha_tramite | tipo_participante | nombre | tramite)
    """
    raw = "|".join([
        normalize_cell(notif.rol),
        normalize_cell(notif.tipo_notif),
        normalize_cell(notif.fecha_tramite),
        normalize_cell(notif.tipo_participante),
        normalize_cell(notif.nombre),
        normalize_cell(notif.tramite),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


def escrito_natural_key(escrito: Any) -> str:
    """Derive a stable 64-char hex key for a PJUDEscrito (row hash).

    All cell values (including boolean flags) are normalised and joined.
    """
    raw = "|".join([
        normalize_cell(escrito.fecha_ingreso),
        normalize_cell(escrito.tipo_escrito),
        normalize_cell(escrito.solicitante),
        str(escrito.tiene_documento),
        str(escrito.tiene_anexo),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# EntitySyncSpec — configuration object for the generic _sync_entities engine
# ---------------------------------------------------------------------------

@dataclass
class EntitySyncSpec:
    """Configuration for one entity type in the generic _sync_entities engine (ADR-001).

    Encodes the per-entity differences as data so the engine loop is written
    once and shared across all entity types.  Adding a new entity type later
    is a new EntitySyncSpec instance, not a new sync_* method.
    """

    model: type                              # SQLAlchemy model class
    entity_type: str                         # "litigante", "notificacion", etc.
    natural_key_fn: Callable[[Any], str]     # (pjud_item) -> 64-char hex key
    to_model_fields: Callable[[Any, int], dict]  # (pjud_item, case_id) -> field dict
    creates_alert: bool = False
    notify: bool = False
    updatable_fields: List[str] = field(default_factory=list)
    # Fields NOT in the natural_key that should be refreshed on the existing row.

    # Slice 2: alert content and notification dispatch wiring
    alert_title_fn: Optional[Callable] = None    # (case, row) -> str
    alert_message_fn: Optional[Callable] = None  # (case, row) -> str
    notify_fn_name: str = ""                     # method name on NotificationService
    event: str = ""                              # webhook event string
    priority: int = 0                            # drain order within shared budget


def _parse_date_for_entity(date_str: Optional[str]) -> Optional[datetime]:
    """Parse a DD/MM/YYYY date string into a datetime (None if blank/invalid)."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Spec instances — all Slice 1b with creates_alert=False, notify=False
# ---------------------------------------------------------------------------

SPEC_LITIGANTE = EntitySyncSpec(
    model=CaseLitigante,
    entity_type="litigante",
    natural_key_fn=litigante_natural_key,
    to_model_fields=lambda item, case_id: {
        "case_id": case_id,
        "participante": item.participante,
        "rut": item.rut,
        "persona_type": item.persona_type,
        "nombre": item.nombre,
    },
    # ADR-004: litigantes are SILENT — no alert, no notification, ever.
    creates_alert=False,
    notify=False,
)

SPEC_NOTIFICACION = EntitySyncSpec(
    model=CaseNotificacion,
    entity_type="notificacion",
    natural_key_fn=notificacion_natural_key,
    to_model_fields=lambda item, case_id: {
        "case_id": case_id,
        "rol": item.rol,
        "estado_notif": item.estado_notif,
        "tipo_notif": item.tipo_notif,
        "fecha_tramite": _parse_date_for_entity(item.fecha_tramite),
        "tipo_participante": item.tipo_participante,
        "nombre": item.nombre,
        "tramite": item.tramite,
        "obs_fallida": item.obs_fallida,
    },
    creates_alert=True,  # Slice 2: flipped from False
    notify=True,         # Slice 2: flipped from False
    updatable_fields=["estado_notif", "obs_fallida"],
    alert_title_fn=lambda case, row: f"Nueva notificación en {case.rol}",
    alert_message_fn=lambda case, row: f"{row.tipo_notif}: {row.nombre}",
    notify_fn_name="notify_new_notificacion",
    event="notificacion.created",
    priority=1,
)

SPEC_ESCRITO = EntitySyncSpec(
    model=CaseEscrito,
    entity_type="escrito",
    natural_key_fn=escrito_natural_key,
    to_model_fields=lambda item, case_id: {
        "case_id": case_id,
        "fecha_ingreso": _parse_date_for_entity(item.fecha_ingreso),
        "tipo_escrito": item.tipo_escrito,
        "solicitante": item.solicitante,
        "tiene_documento": item.tiene_documento,
        "tiene_anexo": item.tiene_anexo,
        "doc_token": item.doc_token,
    },
    creates_alert=True,  # Slice 2: flipped from False
    notify=True,         # Slice 2: flipped from False
    updatable_fields=["doc_token"],
    alert_title_fn=lambda case, row: f"Nuevo escrito en {case.rol}",
    alert_message_fn=lambda case, row: f"{row.tipo_escrito}: {row.solicitante}",
    notify_fn_name="notify_new_escrito",
    event="escrito.created",
    priority=2,
)

SPEC_EXHORTO = EntitySyncSpec(
    model=CaseExhorto,
    entity_type="exhorto",
    natural_key_fn=exhorto_natural_key,
    to_model_fields=lambda item, case_id: {
        "case_id": case_id,
        "rol_origen": item.rol_origen,
        "tipo_exhorto": item.tipo_exhorto,
        "rol_destino": item.rol_destino,
        "fecha_ordena": _parse_date_for_entity(item.fecha_ordena),
        "fecha_ingreso": _parse_date_for_entity(item.fecha_ingreso),
        "tribunal_destino": item.tribunal_destino,
        "estado": item.estado,
    },
    creates_alert=True,  # Slice 2: flipped from False
    notify=True,         # Slice 2: flipped from False
    updatable_fields=["estado"],
    alert_title_fn=lambda case, row: f"Nuevo exhorto en {case.rol}",
    alert_message_fn=lambda case, row: (
        f"{row.tipo_exhorto}: {row.rol_destino} → {row.tribunal_destino}"
    ),
    notify_fn_name="notify_new_exhorto",
    event="exhorto.created",
    priority=3,
)


# ---------------------------------------------------------------------------
# Generic _sync_entities engine
# ---------------------------------------------------------------------------

def _sync_entities(
    db: Session,
    case_id: int,
    scraped_list: list,
    spec: EntitySyncSpec,
    *,
    case: Optional[Any] = None,
    lawyer: Optional[Any] = None,
    webhooks: Optional[list] = None,
    notification_svc: Optional[Any] = None,
    budget: Optional[NotifyBudget] = None,
) -> list:
    """Upsert a list of scraped PJUD entity objects for a given case.

    Performs a SELECT-then-INSERT per item keyed on (case_id, natural_key).
    Existing rows have their spec.updatable_fields refreshed.  Idempotent
    when data is unchanged.

    DOES NOT COMMIT — callers are responsible for the surrounding transaction.

    Slice 2 keyword args (all optional — backward compatible):
        case            Case ORM row; required for alert creation.
        lawyer          Lawyer ORM row; required for alert creation.
        webhooks        Active Webhook list; forwarded to the notify method.
        notification_svc NotificationService instance; required for dispatch.
        budget          Shared NotifyBudget; gates notification dispatch (ADR-005).
                        Alerts are ALWAYS created when spec.creates_alert=True;
                        only the email/webhook dispatch is budget-gated.

    Alert creation is skipped when case or lawyer is None (safe for callers that
    only need storage and omit the notification context).

    Returns:
        List of (row, is_new) tuples — one per input item.
        is_new=True only for genuine inserts; an updatable-field refresh on an
        existing row yields is_new=False.
    """
    results: list = []
    alerts_created = 0
    dispatched = 0

    for item in scraped_list:
        key = spec.natural_key_fn(item)
        existing = db.query(spec.model).filter(
            spec.model.case_id == case_id,
            spec.model.natural_key == key,
        ).first()
        if existing:
            if spec.updatable_fields:
                fields = spec.to_model_fields(item, case_id)
                for col in spec.updatable_fields:
                    if col in fields:
                        setattr(existing, col, fields[col])
                db.flush()
            results.append((existing, False))
            continue

        # New row — insert and assign PK within the outer transaction
        fields = spec.to_model_fields(item, case_id)
        fields["natural_key"] = key
        fields["created_at"] = datetime.utcnow()
        row = spec.model(**fields)
        db.add(row)
        db.flush()

        # Slice 2: create Alert when spec.creates_alert and context is available.
        # Guard: silently skip when case/lawyer is absent (backward compat for
        # storage-only callers that never pass notification context).
        alert = None
        if spec.creates_alert and case is not None and lawyer is not None:
            title = (
                spec.alert_title_fn(case, row)
                if spec.alert_title_fn
                else f"Nuevo {spec.entity_type} detectado"
            )
            message = (
                spec.alert_message_fn(case, row)
                if spec.alert_message_fn
                else ""
            )
            alert = Alert(
                lawyer_id=lawyer.id,
                case_id=case_id,
                entity_type=spec.entity_type,
                entity_id=row.id,
                type=f"new_{spec.entity_type}",
                title=title,
                message=message,
                created_at=datetime.utcnow(),
            )
            db.add(alert)
            db.flush()
            alerts_created += 1

        # Slice 2: dispatch notification when budget allows.
        # Alerts are already persisted above; only DISPATCH is budget-gated.
        if (
            alert is not None
            and spec.notify
            and notification_svc is not None
            and budget is not None
            and not budget.exhausted()
        ):
            try:
                notify_fn = getattr(notification_svc, spec.notify_fn_name)
                notify_fn(case, row, lawyer, webhooks or [], alert)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to dispatch %s notification for entity %s (case_id=%s): %s",
                    spec.entity_type, row.id, case_id, exc,
                )
            budget.decrement()
            dispatched += 1

        results.append((row, True))

    # Cap-reached warning when the budget prevented some dispatches.
    if spec.notify and alerts_created > dispatched:
        skipped = alerts_created - dispatched
        logger.warning(
            "Notification cap reached in _sync_entities "
            "(entity_type=%s, case_id=%s): dispatched %d of %d, skipped %d.",
            spec.entity_type, case_id, dispatched, alerts_created, skipped,
        )

    return results


# ---------------------------------------------------------------------------
# Convenience wrappers — one per entity type
# ---------------------------------------------------------------------------

def upsert_litigantes(
    db: Session, case_id: int, items: list
) -> list:
    """Upsert PJUDLitigante objects for a case. Returns [(row, is_new), ...]."""
    return _sync_entities(db, case_id, items, SPEC_LITIGANTE)


def upsert_notificaciones(
    db: Session, case_id: int, items: list
) -> list:
    """Upsert PJUDNotificacion objects for a case. Returns [(row, is_new), ...]."""
    return _sync_entities(db, case_id, items, SPEC_NOTIFICACION)


def upsert_escritos(
    db: Session, case_id: int, items: list
) -> list:
    """Upsert PJUDEscrito objects for a case. Returns [(row, is_new), ...]."""
    return _sync_entities(db, case_id, items, SPEC_ESCRITO)


def upsert_exhortos(
    db: Session, case_id: int, items: list
) -> list:
    """Upsert PJUDExhorto objects for a case. Returns [(row, is_new), ...]."""
    return _sync_entities(db, case_id, items, SPEC_EXHORTO)


def sync_case_detail(db: Session, case_id: int, detail: Any) -> dict:
    """Persist all entity types for a case detail in a single atomic commit.

    Calls the four upsert_* helpers (each flush-only) and commits once at the
    end so litigantes, notificaciones, escritos, and exhortos for a case all
    persist atomically.  If any upsert raises, no entities for this case are
    committed (the caller's SQLAlchemy transaction rolls back on exception).

    Args:
        db:      Active SQLAlchemy session.
        case_id: Primary key of the Case row to associate entities with.
        detail:  PJUDCaseDetail returned by the scraper.

    Returns:
        Dict with new-row counts per entity type, e.g.
        {"litigantes_new": 3, "notificaciones_new": 0, ...}
    """
    lit_results = upsert_litigantes(db, case_id, detail.litigantes)
    notif_results = upsert_notificaciones(db, case_id, detail.notificaciones)
    escrito_results = upsert_escritos(db, case_id, detail.escritos)
    exhorto_results = upsert_exhortos(db, case_id, detail.exhortos)
    db.commit()
    return {
        "litigantes_new": sum(1 for _, is_new in lit_results if is_new),
        "notificaciones_new": sum(1 for _, is_new in notif_results if is_new),
        "escritos_new": sum(1 for _, is_new in escrito_results if is_new),
        "exhortos_new": sum(1 for _, is_new in exhorto_results if is_new),
    }


class SyncService:
    """
    Synchronize scraped PJUD data with local database.
    
    Usage:
        sync = SyncService(db)
        result = sync.sync_cases(lawyer_id, scraped_cases, competencia="civil")
    """
    
    def __init__(self, db: Session):
        self.db = db
        # Per-instance court cache (ADR: batch N+1 fix). SyncService is
        # instantiated fresh per sync run at every call site, so this is
        # effectively "per sync run" without needing an explicit reset hook.
        self._court_cache: dict = {}
    
    def sync_cases(
        self,
        lawyer_id: int,
        scraped_cases: List[ScrapedCase],
        competencia: str = "civil",
        year: Optional[str] = None,
        triggered_by: str = "manual",
    ) -> SyncResult:
        """
        Sync a list of scraped cases to the database.
        
        Args:
            lawyer_id: The lawyer who owns these cases
            scraped_cases: List of cases from PJUD scraper
            competencia: civil, laboral, or penal
            year: Optional year filter
            triggered_by: manual, scheduled, or webhook
        
        Returns:
            SyncResult with counts of what was synced
        """
        # Create sync history record
        sync_record = SyncHistory(
            lawyer_id=lawyer_id,
            competencia=competencia,
            year=year,
            started_at=datetime.utcnow(),
            triggered_by=triggered_by,
        )
        self.db.add(sync_record)
        self.db.flush()

        result = SyncResult(cases_total=len(scraped_cases))

        # N+1 fix: pre-load all of this lawyer's existing cases in ONE query
        # instead of one SELECT per scraped case. Matches the exact filter
        # the old per-case query used (lawyer_id + rol, NOT competencia —
        # the unique constraint is (lawyer_id, rol), so a case can exist
        # under a different competencia than the one being synced right now
        # and must still be matched/updated, not duplicated).
        existing_by_rol = {
            c.rol: c
            for c in self.db.query(Case).filter(Case.lawyer_id == lawyer_id).all()
        }

        # Chunked commits (resilience): a lawyer with a large caseload (e.g.
        # ~2085 cases) synced over the Mac's Cloud SQL Auth Proxy can hit a
        # dropped connection mid-commit if everything is committed in one
        # shot at the end. Committing every SYNC_COMMIT_CHUNK_SIZE processed
        # cases bounds the blast radius of a drop to at most one chunk
        # instead of the whole batch, plus one final commit that finalizes
        # the SyncHistory record.
        chunk_size = max(1, settings.SYNC_COMMIT_CHUNK_SIZE)
        total = len(scraped_cases)

        # expire_on_commit gotcha: the default Session expires every
        # persistent object on commit. With chunked interim commits that
        # would expire self._court_cache's Court objects and the
        # existing_by_rol Case objects pre-loaded above — the next attribute
        # access on a cache/map hit (e.g. `court.id`) would trigger a reload
        # SELECT, silently reintroducing the N+1 pattern across chunk
        # boundaries. Disable it for the duration of this sync run only
        # (restored in `finally`) so cached/pre-loaded objects survive
        # interim commits without a reload.
        original_expire_on_commit = self.db.expire_on_commit
        self.db.expire_on_commit = False
        try:
            for start in range(0, total, chunk_size):
                chunk = scraped_cases[start : start + chunk_size]

                def _process_chunk(chunk=chunk):
                    chunk_new = 0
                    chunk_updated = 0
                    chunk_errors: List[str] = []
                    for scraped in chunk:
                        try:
                            case, is_new = self._upsert_case(
                                lawyer_id, scraped, competencia, existing_by_rol
                            )
                            if is_new:
                                chunk_new += 1
                            else:
                                chunk_updated += 1
                        except Exception as e:
                            chunk_errors.append(f"Error syncing {scraped.rol}: {str(e)}")
                    return chunk_new, chunk_updated, chunk_errors

                chunk_new, chunk_updated, chunk_errors = self._commit_with_retry(
                    _process_chunk, caches=(existing_by_rol, self._court_cache)
                )
                result.cases_new += chunk_new
                result.cases_updated += chunk_updated
                result.errors.extend(chunk_errors)

            def _finalize_sync_record():
                # Guards the (rare) case where this is the very first commit
                # of the whole run (e.g. zero scraped_cases -> no chunks
                # above) and a prior failed attempt's rollback expunged the
                # still-uncommitted sync_record — re-attach before re-setting.
                self.db.add(sync_record)
                sync_record.cases_found = result.cases_total
                sync_record.cases_new = result.cases_new
                sync_record.cases_updated = result.cases_updated
                sync_record.complete(
                    status="completed" if not result.errors else "partial",
                    error="; ".join(result.errors) if result.errors else None,
                )

            self._commit_with_retry(_finalize_sync_record)
        finally:
            self.db.expire_on_commit = original_expire_on_commit

        return result

    def _commit_with_retry(self, work_fn: Callable[[], T], caches: Tuple[dict, ...] = ()) -> T:
        """(Re)apply ``work_fn()`` and commit, retrying on OperationalError.

        A dropped Cloud SQL Auth Proxy connection during a long commit
        surfaces as ``sqlalchemy.exc.OperationalError`` (e.g. "server closed
        the connection unexpectedly"). ``pool_pre_ping=True`` (see
        ``app.core.database``) discards the dead connection and grabs a
        fresh one from the pool before the retry.

        Why ``work_fn`` is redone on every attempt (not just the bare
        commit): ``self.db.rollback()`` undoes everything pending in the
        transaction — any ORM object that was only "pending" (added +
        flushed but never committed) is expunged from the session, and any
        already-persistent object touched in this transaction is expired.
        A blind retry of ``self.db.commit()`` alone would therefore commit
        nothing (the work was wiped by the rollback). Because case/court
        upserts and the SyncHistory field assignments are idempotent, redoing
        ``work_fn()`` before each retry is always safe.

        ``caches`` are dicts (e.g. ``existing_by_rol``, ``self._court_cache``)
        that ``work_fn`` may mutate in place (inserting newly created rows so
        a later item in the same unit — or a later chunk — resolves to them
        instead of re-querying/re-creating). They are snapshotted before each
        attempt and restored verbatim on failure, so a partially-applied
        failed attempt never leaves a stale (expunged) entry behind for the
        redo to trip over.

        Only OperationalError is retried — any other exception (e.g. a
        genuine integrity/programming error) propagates immediately without
        a retry, since it is not a transient connectivity issue.
        """
        max_attempts = max(1, settings.SYNC_COMMIT_MAX_RETRIES)
        for attempt in range(1, max_attempts + 1):
            snapshots = [dict(cache) for cache in caches]
            try:
                result = work_fn()
                self.db.commit()
                return result
            except OperationalError:
                self.db.rollback()
                for cache, snapshot in zip(caches, snapshots):
                    cache.clear()
                    cache.update(snapshot)
                if attempt >= max_attempts:
                    raise
                logger.warning(
                    "sync_cases: commit attempt %d/%d failed with "
                    "OperationalError, retrying after rollback",
                    attempt,
                    max_attempts,
                )
    
    def get_last_sync(self, lawyer_id: int, competencia: str) -> Optional[SyncHistory]:
        """Get the last successful sync for a lawyer/competencia."""
        return self.db.query(SyncHistory).filter(
            and_(
                SyncHistory.lawyer_id == lawyer_id,
                SyncHistory.competencia == competencia,
                SyncHistory.status.in_(["completed", "partial"]),
            )
        ).order_by(SyncHistory.completed_at.desc()).first()
    
    def needs_sync(self, lawyer_id: int, competencia: str, max_age_hours: int = 4) -> bool:
        """Check if a sync is needed based on last sync time."""
        last_sync = self.get_last_sync(lawyer_id, competencia)
        if not last_sync or not last_sync.completed_at:
            return True
        
        age = datetime.utcnow() - last_sync.completed_at
        return age.total_seconds() > (max_age_hours * 3600)
    
    def sync_movements(
        self,
        case_id: int,
        scraped_movements: List[ScrapedMovement],
        *,
        budget: Optional[NotifyBudget] = None,
    ) -> Tuple[int, int]:
        """Sync movements for a case, detecting new ones.

        Args:
            case_id: Database case ID.
            scraped_movements: Movements from PJUD scraper.
            budget: Optional shared NotifyBudget (ADR-005).  When provided, the
                    caller's budget is drained instead of a local one.  When
                    omitted (existing callers), a fresh local budget is created
                    from settings.NOTIFY_MAX_PER_SYNC — backward compatible.

        Returns:
            Tuple of (new_count, alert_count).

        Recipient resolution (Approach C, ADR-005): fans out to every
        abogado-of-record litigante on the case via
        ``resolve_case_alert_recipients`` — one Alert + one notification
        dispatch per recipient, de-duped by lawyer id (a lawyer appearing
        under two litigante rows still gets exactly one delivery). Falls
        back to the case's existing owner lawyer (``case.lawyer_id``) when
        no internal recipient resolves yet (bootstrap window before
        litigantes are scraped, or only opposing/external counsel present)
        so an alert is never silently dropped. ``NotifyBudget`` is drained
        per DISPATCH (i.e. per recipient), not per case/movement.
        """
        new_count = 0
        alert_count = 0
        notify_dispatched = 0

        case = self.db.query(Case).filter(Case.id == case_id).first()
        if not case:
            return 0, 0

        # Hoist recipient resolution + active-Webhook queries outside the
        # per-movement loop (constant for the whole call → removes O(N)
        # round-trips).
        from app.services.lawyer_roster import resolve_case_alert_recipients

        recipients = resolve_case_alert_recipients(self.db, case)
        if not recipients:
            fallback_lawyer = (
                self.db.query(Lawyer).filter(Lawyer.id == case.lawyer_id).first()
            )
            recipients = [fallback_lawyer] if fallback_lawyer else []

        recipient_webhooks: dict = {}
        for recipient in recipients:
            recipient_webhooks[recipient.id] = self.db.query(Webhook).filter(
                Webhook.lawyer_id == recipient.id,
                Webhook.is_active == True,  # noqa: E712
            ).all()

        # Use the caller's shared budget or create a local one (backward compat).
        _budget = budget if budget is not None else NotifyBudget.from_settings()

        for scraped in scraped_movements:
            movement, is_new = self._upsert_movement(case_id, scraped)
            if is_new:
                new_count += 1
                for recipient in recipients:
                    # Alert is always persisted; only DISPATCH is budget-gated.
                    alert = self._create_movement_alert(case, movement, recipient.id)
                    if not alert:
                        continue
                    alert_count += 1
                    if not _budget.exhausted():
                        try:
                            NotificationService(self.db).notify_new_movement(
                                alert, case, movement, recipient,
                                recipient_webhooks[recipient.id],
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed to dispatch notification for alert on "
                                "case %s movement %s recipient %s: %s",
                                case.id, movement.id, recipient.id, exc,
                            )
                        # Count every attempt (success or error) toward the cap.
                        _budget.decrement()
                        notify_dispatched += 1

        # Emit a warning when the cap prevented some dispatches.
        if alert_count > notify_dispatched:
            skipped = alert_count - notify_dispatched
            logger.warning(
                "Notification cap reached in sync_movements (case_id=%s): "
                "dispatched %d of %d notifications, skipped %d. "
                "Increase NOTIFY_MAX_PER_SYNC (currently %d) to raise the limit.",
                case_id,
                notify_dispatched,
                alert_count,
                skipped,
                _budget.remaining + notify_dispatched,  # original limit
            )

        # Update last_movement_at on case
        if scraped_movements:
            latest_date = self._parse_date(scraped_movements[0].fecha)
            if latest_date and (
                not case.last_movement_at or latest_date > case.last_movement_at
            ):
                case.last_movement_at = latest_date

        self.db.commit()
        return new_count, alert_count
    
    def _upsert_case(
        self,
        lawyer_id: int,
        scraped: ScrapedCase,
        competencia: str,
        existing_by_rol: dict,
    ) -> Tuple[Case, bool]:
        """
        Insert or update a case.

        Args:
            existing_by_rol: pre-loaded map of {normalized_rol: Case} for this
                lawyer (see sync_cases), built with ONE query instead of one
                per case. Mutated in place when a new case is created so a
                duplicate ROL later in the same batch resolves to the
                just-created row (mirrors the old behavior, where the
                per-case flush let the next per-case query find it).

        Returns:
            Tuple of (case, is_new)
        """
        # Normalize ROL on write so DB values always match the uppercased form used
        # in rotation queries, preventing starvation from mixed-case PJUD responses.
        normalized_rol = scraped.rol.strip().upper()

        # Find existing case by ROL and lawyer (from the pre-loaded map).
        existing = existing_by_rol.get(normalized_rol)

        # Get or create court
        court = self._get_or_create_court(scraped.tribunal, competencia)
        
        # Parse caratulado into plaintiff/defendant
        plaintiff, defendant = self._parse_caratulado(scraped.caratulado)
        
        # Parse fecha_ingreso
        filed_at = self._parse_date(scraped.fecha_ingreso)
        
        if existing:
            # Update existing case
            existing.court_id = court.id
            existing.plaintiff = plaintiff
            existing.defendant = defendant
            existing.status = self._map_status(scraped.estado_cuaderno)
            existing.procedure = scraped.cuaderno
            existing.competencia = competencia
            if filed_at:
                existing.filed_at = filed_at
            existing.updated_at = datetime.utcnow()
            return existing, False
        else:
            # Create new case
            case = Case(
                lawyer_id=lawyer_id,
                court_id=court.id,
                rol=normalized_rol,
                plaintiff=plaintiff,
                defendant=defendant,
                status=self._map_status(scraped.estado_cuaderno),
                procedure=scraped.cuaderno,
                competencia=competencia,
                filed_at=filed_at,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            self.db.add(case)
            self.db.flush()  # Get the ID
            # Make the just-created case visible to later scraped entries in
            # this same batch that share the same ROL (duplicate-ROL guard).
            existing_by_rol[normalized_rol] = case
            return case, True
    
    def _upsert_movement(
        self,
        case_id: int,
        scraped: ScrapedMovement,
    ) -> Tuple[Movement, bool]:
        """
        Insert movement if it doesn't exist.
        
        Returns:
            Tuple of (movement, is_new)
        """
        movement_date = self._parse_date(scraped.fecha)
        
        # Check if movement already exists (by folio + date + description)
        existing = self.db.query(Movement).filter(
            and_(
                Movement.case_id == case_id,
                Movement.folio == scraped.folio,
                Movement.description == scraped.descripcion,
            )
        ).first()
        
        if existing:
            return existing, False
        
        # Create new movement
        movement = Movement(
            case_id=case_id,
            folio=scraped.folio,
            stage=scraped.etapa,
            procedure=scraped.tipo_tramite,
            description=scraped.descripcion,
            movement_date=movement_date or datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
        self.db.add(movement)
        self.db.flush()
        return movement, True
    
    def _create_movement_alert(
        self, case: Case, movement: Movement, lawyer_id: int
    ) -> Optional[Alert]:
        """Create an alert for a new movement, addressed to one recipient.

        Approach C (ADR-005): the caller (``sync_movements``) fans out to
        every resolved recipient, so this creates exactly ONE Alert for
        ``lawyer_id`` — never assumes ``case.lawyer_id`` is the recipient.
        """
        alert = Alert(
            lawyer_id=lawyer_id,
            case_id=case.id,
            movement_id=movement.id,
            type="new_movement",
            title=f"Nuevo movimiento en {case.rol}",
            message=f"{movement.procedure}: {movement.description}",
            created_at=datetime.utcnow(),
        )
        self.db.add(alert)
        return alert
    
    def _get_or_create_court(self, tribunal_name: str, competencia: str) -> Court:
        """Get or create a court by name.

        N+1 fix: courts repeat heavily across a lawyer's caseload, so this
        checks an in-memory cache (self._court_cache) before hitting the DB.
        Both DB-fetched and newly-created courts are cached, so the 2nd+
        case sharing a tribunal is a cache hit, not another query.
        """
        # Normalize name
        name = tribunal_name.strip()

        cached = self._court_cache.get(name)
        if cached is not None:
            return cached

        existing = self.db.query(Court).filter(Court.name == name).first()
        if existing:
            self._court_cache[name] = existing
            return existing

        # Create new court
        # Generate code from name (e.g., "24º Juzgado Civil de Santiago" -> "24JCS")
        code = self._generate_court_code(name)

        court = Court(
            code=code,
            name=name,
            region=self._extract_region(name),
            type=competencia,
        )
        self.db.add(court)
        self.db.flush()
        self._court_cache[name] = court
        return court
    
    def _parse_caratulado(self, caratulado: str) -> Tuple[str, str]:
        """
        Parse caratulado into plaintiff and defendant.
        
        Example: "BANCO ITAU/FERNÁNDEZ" -> ("BANCO ITAU", "FERNÁNDEZ")
        """
        if "/" in caratulado:
            parts = caratulado.split("/", 1)
            return parts[0].strip(), parts[1].strip()
        return caratulado, ""
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse date string from PJUD format (DD/MM/YYYY)."""
        if not date_str:
            return None
        
        try:
            return datetime.strptime(date_str, "%d/%m/%Y")
        except ValueError:
            try:
                return datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return None
    
    def _map_status(self, estado_cuaderno: str) -> str:
        """Map PJUD estado to internal status."""
        estado_lower = estado_cuaderno.lower() if estado_cuaderno else ""
        
        if "archivado" in estado_lower or "terminado" in estado_lower:
            return "closed"
        elif "suspendido" in estado_lower:
            return "suspended"
        else:
            return "active"
    
    def _generate_court_code(self, name: str) -> str:
        """Generate a unique code for a court."""
        import re
        import hashlib
        
        # Try to extract number and type
        # "24º Juzgado Civil de Santiago" -> "24JCS"
        match = re.search(r"(\d+)", name)
        number = match.group(1) if match else ""
        
        # Get first letter of each significant word
        words = re.findall(r"[A-Z][a-z]+", name)
        initials = "".join(w[0] for w in words[:3])
        
        # Append a deterministic hash of the full name so that two distinct
        # courts sharing the same number+initials do NOT collide on the unique
        # `code` column (e.g. "1º Juzgado Civil de Santiago" and
        # "1º Juzgado Civil de San Miguel" both reduce to "1JCS").
        suffix = hashlib.md5(name.encode()).hexdigest()[:8].upper()
        code = f"{number}{initials}{suffix}"

        return code[:20]
    
    def _extract_region(self, court_name: str) -> str:
        """Extract region from court name."""
        # Common regions in court names
        regions = {
            "Santiago": "RM",
            "Valparaíso": "V",
            "Viña del Mar": "V",
            "Concepción": "VIII",
            "Temuco": "IX",
            "Puerto Montt": "X",
            "Antofagasta": "II",
            "La Serena": "IV",
            "Rancagua": "VI",
            "Talca": "VII",
            "Valdivia": "XIV",
            "Punta Arenas": "XII",
        }
        
        for city, region in regions.items():
            if city.lower() in court_name.lower():
                return region
        
        return "RM"  # Default to Santiago


def convert_api_cases_to_scraped(api_cases: list) -> List[ScrapedCase]:
    """
    Convert API response cases to ScrapedCase objects.
    
    Args:
        api_cases: List of dicts from API response
    
    Returns:
        List of ScrapedCase objects
    """
    return [
        ScrapedCase(
            rol=c.get("rol", ""),
            tribunal=c.get("tribunal", ""),
            caratulado=c.get("caratulado", ""),
            fecha_ingreso=c.get("fecha_ingreso", ""),
            estado_cuaderno=c.get("estado_cuaderno", ""),
            cuaderno=c.get("cuaderno", ""),
            institucion=c.get("institucion"),
        )
        for c in api_cases
    ]


def convert_api_movements_to_scraped(api_movements: list) -> List[ScrapedMovement]:
    """
    Convert API response movements to ScrapedMovement objects.
    
    Args:
        api_movements: List of dicts from API response
    
    Returns:
        List of ScrapedMovement objects
    """
    return [
        ScrapedMovement(
            folio=m.get("folio", ""),
            fecha=m.get("fecha", ""),
            tipo_tramite=m.get("tipo_tramite", ""),
            descripcion=m.get("descripcion", ""),
            etapa=m.get("etapa"),
            foja=m.get("foja"),
            tiene_documento=m.get("tiene_documento", False),
        )
        for m in api_movements
    ]


async def fetch_case_details_parallel(
    case_ids: List[str],
    fetch_func: Callable[[str], Awaitable[T]],
    max_concurrent: int = MAX_CONCURRENT_FETCHES,
) -> List[Tuple[str, Optional[T], Optional[str]]]:
    """
    Fetch case details in parallel with limited concurrency.
    
    Args:
        case_ids: List of case ROLs to fetch
        fetch_func: Async function that takes a ROL and returns case details
        max_concurrent: Maximum concurrent fetches (default: 3)
    
    Returns:
        List of tuples: (rol, result, error_message)
        - result is None if fetch failed
        - error_message is None if fetch succeeded
    
    Example:
        async def fetch_detail(rol: str) -> dict:
            async with BrowserFactory() as factory:
                page = await factory.new_page(session)
                scraper = CivilScraper(page=page)
                return await scraper.get_case_detail(rol)
        
        results = await fetch_case_details_parallel(rols, fetch_detail)
        for rol, detail, error in results:
            if detail:
                process_detail(rol, detail)
            else:
                log_error(rol, error)
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def fetch_with_semaphore(rol: str) -> Tuple[str, Optional[T], Optional[str]]:
        async with semaphore:
            try:
                logger.debug(f"Fetching case detail: {rol}")
                result = await fetch_func(rol)
                logger.debug(f"Fetched case detail: {rol}")
                return (rol, result, None)
            except Exception as e:
                logger.warning(f"Failed to fetch case {rol}: {e}")
                return (rol, None, str(e))
    
    logger.info(f"Fetching {len(case_ids)} case details (max {max_concurrent} concurrent)")
    tasks = [fetch_with_semaphore(rol) for rol in case_ids]
    results = await asyncio.gather(*tasks)
    
    successful = sum(1 for _, result, _ in results if result is not None)
    logger.info(f"Fetched {successful}/{len(case_ids)} case details successfully")

    return results


# Default cap on cases processed for movement detection in an on-demand sync
MOVEMENT_CHECK_DEFAULT_MAX = 5


def _select_cases_for_movement_check(
    api_cases: list,
    rol: Optional[str],
    max_cases: int = MOVEMENT_CHECK_DEFAULT_MAX,
) -> list:
    """
    Choose which scraped cases to run movement detection on.

    Args:
        api_cases: List of PJUDCase objects returned by get_my_cases.
        rol: If provided, return only the case whose .rol matches.
             If None, return at most *max_cases* from the front of the list.
        max_cases: Cap when no rol filter is given (default: 5).

    Returns:
        Filtered/capped list of PJUDCase objects.
    """
    if rol:
        # Normalize both sides (strip + upper) so format/whitespace
        # mismatches don't silently return [] and skip movement detection.
        normalized_rol = rol.strip().upper()
        return [c for c in api_cases if c.rol.strip().upper() == normalized_rol]
    return api_cases[:max_cases]


def _select_cases_for_detail_rotation(
    db: Session,
    lawyer_id: int,
    competencia: str,
    api_cases: list,
    batch_size: int,
    reserved_first: bool = False,
) -> list:
    """Select PJUDCase objects for detail scraping via DB-driven rotation.

    Queries Case rows for this lawyer + competencia ordered by
    ``last_detail_checked_at ASC NULLS FIRST, filed_at DESC`` with a LIMIT of
    *batch_size*.  Each DB row is matched to *api_cases* by normalized ROL so
    the returned objects carry the live ``case_token`` required for scraping.

    Args:
        db: Active SQLAlchemy session.
        lawyer_id: Filter to this lawyer's cases.
        competencia: Filter to this competencia (e.g. "civil").
        api_cases: Live PJUD case list from ``get_my_cases``.
        batch_size: Maximum number of cases to return.

    Returns:
        List of PJUDCase objects (subset of *api_cases*) in rotation order,
        skipping DB cases absent from *api_cases* or without a ``case_token``.
        If no Case rows exist in DB for this lawyer+competencia, returns
        ``api_cases[:batch_size]`` so the caller is never starved.
    """
    # Year scope: only detail-scrape cases whose ROL year (the YYYY in "C-N-YYYY")
    # is >= settings.DETAIL_MIN_YEAR. 0 disables the filter. Unparseable ROLs are
    # NOT excluded (fail-open) so a format quirk never silently drops a case.
    min_year = settings.DETAIL_MIN_YEAR

    def _year_ok(rol: str) -> bool:
        if min_year <= 0:
            return True
        try:
            last = rol.rsplit("-", 1)[-1]
        except (AttributeError, IndexError):
            return True
        # Only enforce the filter when the suffix is a real 4-digit year (YYYY).
        # Non-standard ROLs fail open so a format quirk never silently drops a case.
        if len(last) != 4 or not last.isdigit():
            return True
        return int(last) >= min_year

    # Build lookup: normalized_rol → api_case (live token + within the year scope).
    api_lookup = {
        ac.rol.strip().upper(): ac
        for ac in api_cases
        if ac.case_token and _year_ok(ac.rol)
    }

    # Year-scoped fallback used when DB rotation can't be applied.
    fallback = [ac for ac in api_cases if ac.case_token and _year_ok(ac.rol)][:batch_size]

    # Rotation order; the year filter + batch cap are applied in Python below
    # (parsing the ROL year in SQL is not portable across Postgres/SQLite).
    order_clauses = []
    if reserved_first:
        order_clauses.append(Case.consulta_reserved.desc())
    order_clauses.extend([
        Case.last_detail_checked_at.asc().nullsfirst(),
        Case.filed_at.desc(),
    ])
    db_cases = (
        db.query(Case)
        .filter(Case.lawyer_id == lawyer_id, Case.competencia == competencia)
        .order_by(*order_clauses)
        .all()
    )

    # Empty DB for this lawyer+competencia — fall back to live cases.
    if not db_cases:
        return fallback

    result = []
    for db_case in db_cases:
        if not _year_ok(db_case.rol):
            continue
        api_case = api_lookup.get(db_case.rol.strip().upper())
        if api_case is not None:
            result.append(api_case)
        if len(result) >= batch_size:
            break

    # Ghost-case guard: DB had rows but none matched live api_cases (stale/archived
    # cases removed from PJUD).  Those rows have NULL last_detail_checked_at so they
    # sit permanently at the front of the rotation order and starve live cases.
    # Fall back to live api_cases so rotation is not blocked by ghost rows.
    if not result and db_cases:
        return fallback

    return result


def _oldest_unchecked_label(db: Session, lawyer_id: int) -> str:
    """Return a human-readable rotation status label.

    SQL MIN() ignores NULLs, so never-checked cases (NULL last_detail_checked_at)
    are counted explicitly and reported separately from the oldest checked timestamp.
    """
    never_checked = (
        db.query(func.count(Case.id))
        .filter(Case.lawyer_id == lawyer_id, Case.last_detail_checked_at.is_(None))
        .scalar()
        or 0
    )
    oldest_checked = (
        db.query(func.min(Case.last_detail_checked_at))
        .filter(Case.lawyer_id == lawyer_id, Case.last_detail_checked_at.isnot(None))
        .scalar()
    )
    if oldest_checked is None:
        return f"{never_checked} never-checked; oldest-checked: none"
    age = datetime.utcnow() - oldest_checked
    return (
        f"{never_checked} never-checked; "
        f"oldest-checked: {age.days}d {age.seconds // 3600}h ago"
    )


def emit_deadline_alerts(
    db: Session,
    case: Case,
    transition: SemaforoTransition,
    *,
    budget: Optional[NotifyBudget] = None,
) -> int:
    """Fan out ROJO-entry / fatal-deadline alerts for one recompute transition.

    The highest-value gap this closes: ``DeadlineEngine.recompute_case``
    computes the semáforo but never told anyone when a case turned critical.
    This is the small alert-fan-out helper the engine deliberately does NOT
    own (``DeadlineEngine`` stays free of any notification imports) — callers
    (``_maybe_recompute_deadlines``) call this right after recomputing.

    Transition-based = naturally de-duped: an already-ROJO case that
    recomputes and stays ROJO does not re-alert (see
    ``SemaforoTransition.entered_rojo`` / ``.fatal_appeared``). A case
    leaving ROJO never alerts either — only entering it does.

    Mirrors ``sync_movements``'s recipient-resolution + NotifyBudget-gated
    dispatch (ADR-005): one Alert + one notification dispatch per recipient
    resolved via ``resolve_case_alert_recipients``, falling back to the
    case's existing owner lawyer when no internal recipient resolves yet.
    Alerts are ALWAYS persisted; only the email/webhook DISPATCH is
    budget-gated. Never raises — the caller wraps this in a best-effort
    try/except so a notification failure never breaks ingest/sync.

    Returns the number of Alert rows created (0 when there is no qualifying
    transition, or no resolvable recipient).
    """
    if not (transition.entered_rojo or transition.fatal_appeared):
        return 0

    from app.services.lawyer_roster import resolve_case_alert_recipients

    recipients = resolve_case_alert_recipients(db, case)
    if not recipients:
        fallback_lawyer = (
            db.query(Lawyer).filter(Lawyer.id == case.lawyer_id).first()
        )
        recipients = [fallback_lawyer] if fallback_lawyer else []
    if not recipients:
        return 0

    recipient_webhooks: dict = {}
    for recipient in recipients:
        recipient_webhooks[recipient.id] = db.query(Webhook).filter(
            Webhook.lawyer_id == recipient.id,
            Webhook.is_active == True,  # noqa: E712
        ).all()

    _budget = budget if budget is not None else NotifyBudget.from_settings()

    tribunal = case.court.name if case.court else "tribunal no especificado"
    plazo_txt = (
        f"Próximo plazo: {case.next_deadline_at}."
        if case.next_deadline_at
        else "Sin próximo plazo activo registrado."
    )

    events: list[tuple[str, str, str]] = []
    if transition.entered_rojo:
        events.append((
            "semaforo_rojo",
            f"⚠️ Causa {case.rol} pasó a ROJO",
            f"La causa {case.rol} ante {tribunal} pasó a estado ROJO. {plazo_txt}",
        ))
    if transition.fatal_appeared:
        events.append((
            "deadline_fatal",
            f"⏰ Plazo fatal en causa {case.rol}",
            f"La causa {case.rol} ante {tribunal} tiene un plazo fatal próximo. {plazo_txt}",
        ))

    notification_svc = NotificationService(db)
    alert_count = 0
    for alert_type, title, message in events:
        for recipient in recipients:
            alert = Alert(
                lawyer_id=recipient.id,
                case_id=case.id,
                type=alert_type,
                title=title,
                message=message,
                created_at=datetime.utcnow(),
            )
            db.add(alert)
            db.flush()  # assign alert.id before dispatch/logging reference it
            alert_count += 1
            if not _budget.exhausted():
                try:
                    notification_svc.notify_deadline_alert(
                        alert, case, recipient, recipient_webhooks[recipient.id],
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to dispatch deadline alert for case %s "
                        "recipient %s: %s",
                        case.id, recipient.id, exc,
                    )
                _budget.decrement()

    return alert_count


def _maybe_recompute_deadlines(
    db: Session, case: Case, *, budget: Optional[NotifyBudget] = None
) -> None:
    """Recompute procedural deadlines for *case* after a movement sync, then
    fan out a ROJO-entry / fatal-deadline alert on a qualifying transition.

    Called from inside the _do_fetch closure in detect_and_sync_movements,
    from sync_via_consulta, and from IngestService.ingest_movements. Only
    processes civil competencia; silently returns for all others.
    Never re-raises — a deadline computation OR alert-emission failure must
    NOT abort the surrounding sync/ingest transaction.

    Args:
        db:     Active SQLAlchemy session (shared with the sync transaction).
        case:   Case ORM instance (already flushed, not yet committed).
        budget: Optional shared NotifyBudget (ADR-005) — when the caller
                already tracks a per-sync-cycle budget (movements + entity
                alerts), pass it through so deadline alerts drain the same
                pool instead of getting their own independent cap.
    """
    if (case.competencia or "").lower() != "civil":
        return
    try:
        transition = DeadlineEngine.recompute_case(db, case)
    except Exception:
        logger.exception(
            "_maybe_recompute_deadlines failed for case_id=%s; "
            "deadline computation skipped — sync continues",
            getattr(case, "id", "?"),
        )
        return

    try:
        emit_deadline_alerts(db, case, transition, budget=budget)
    except Exception:
        logger.exception(
            "emit_deadline_alerts failed for case_id=%s; "
            "alert skipped — sync continues",
            getattr(case, "id", "?"),
        )


async def detect_and_sync_movements(
    db: Session,
    scraper,
    pjud_session,
    lawyer_id: int,
    api_cases: list,
    rol: Optional[str] = None,
    selected_cases: Optional[list] = None,
    delay_between_fetches: float = 0.0,
    reauth_callback: Optional[Callable[[], Awaitable[Optional["PJUDSession"]]]] = None,
) -> Tuple[int, int, List[str]]:
    """Fetch case details for selected cases and sync new movements to the database.

    This is the canonical movement-detection implementation shared by the
    ``POST /sync`` endpoint (on-demand) and the scheduled worker (autonomous).
    Keeping a single implementation ensures one code path to test and maintain.

    Selection precedence:
    - If *selected_cases* is provided, it is used directly (scheduler rotation path).
    - Otherwise selection is governed by ``_select_cases_for_movement_check``:
      - If *rol* is given, only that case is fetched (targeted/on-demand mode).
      - Otherwise, at most ``MOVEMENT_CHECK_DEFAULT_MAX`` cases from the front of
        the list are fetched (rate-limit-friendly default).

    After each successful ``get_case_detail`` call, ``db_case.last_detail_checked_at``
    is advanced to the current UTC time so the rotation scheduler knows which cases
    were recently checked.  A case-specific error (non-session) also advances the
    timestamp so a persistently-failing case rotates to the back and does not block
    the batch every run.

    Notifications are dispatched automatically by ``SyncService.sync_movements``
    via the existing ``NotificationService`` path (email + HMAC webhooks).

    Args:
        db: Active SQLAlchemy session.
        scraper: Scraper instance with ``get_case_detail(session, case_token)``.
        pjud_session: Active ``PJUDSession`` used for authenticated scraping.
        lawyer_id: Owner of the cases being checked.
        api_cases: List of PJUDCase objects returned by ``get_my_cases``.
        rol: Optional ROL filter — when set, only that case is detail-fetched
            (on-demand path; ignored when *selected_cases* is provided).
        selected_cases: When provided by the scheduler, use this pre-selected
            list directly and bypass the internal ``_select_cases_for_movement_check``
            call.  The ROL-targeted on-demand path passes ``None`` here.
        delay_between_fetches: Seconds to sleep between consecutive detail
            fetches.  Use ``0.0`` (default) for on-demand endpoint calls; use
            a positive value (e.g. ``2.0``) in scheduled workers to be
            considerate toward the PJUD infrastructure.
        reauth_callback: Optional async callable ``() -> Optional[PJUDSession]``
            injected by the scheduler.  When a ``SessionExpiredError`` or
            ``SessionNotAuthenticatedError`` is caught, this callback is invoked
            once to obtain a fresh session.  If it returns ``None`` or raises,
            the batch stops gracefully (remaining cases deferred to the next
            run).  If it returns a valid session, the current case is retried
            once with the new session.  ``last_detail_checked_at`` is NEVER
            advanced on a session error — session failure is not the case's
            fault, and the rotation position must be preserved for the retry.
            NOTE: this callback is NEVER invoked for a ``ShapeChallengeError``
            — see below.

    Shape/TSPD handling:
        A ``ShapeChallengeError`` (subclass of ``SessionNotAuthenticatedError``)
        is caught SEPARATELY and BEFORE the generic session-error handling
        above.  Shape/TSPD flags the egress IP's behavioral reputation — it is
        a station-wide rate/behavioral block, not a per-session expiry.
        Reauthenticating and retrying within seconds (as we do for a genuine
        session expiry) would hammer the flagged IP again during its
        reputation-cooldown window and deepen the block.  On a Shape
        challenge: ``reauth_callback`` is NOT called, the case is NOT
        retried, the process-global ``ShapeCooldown`` (see
        ``app.services.shape_cooldown``) is tripped, and the batch aborts
        immediately (movements synced before the block are still returned).
        Also: if ``ShapeCooldown.active()`` at entry, this function skips all
        detail work for the whole batch and returns early — a Shape hit
        while processing one lawyer backs off every other lawyer's cycle
        too, since Shape flags the shared egress IP.

    Returns:
        Tuple of ``(movements_new, alerts_created, errors)`` where *errors* is
        a list of human-readable strings for any case whose fetch or processing
        failed (the function never raises — errors are accumulated instead).
    """
    movements_new: int = 0
    alerts_created: int = 0
    errors: List[str] = []

    shape_cooldown = get_shape_cooldown()
    if shape_cooldown.active():
        logger.warning(
            "detect_and_sync_movements: in Shape cooldown, %.0fs remaining; "
            "skipping PJUD detail work (lawyer_id=%s)",
            shape_cooldown.remaining(),
            lawyer_id,
        )
        return movements_new, alerts_created, errors

    if selected_cases is not None:
        cases_for_check = selected_cases
    else:
        cases_for_check = _select_cases_for_movement_check(api_cases, rol=rol)

    if not cases_for_check:
        if rol:
            logger.warning(
                "detect_and_sync_movements: rol=%s not found in results; "
                "skipping movement detection",
                rol,
            )
        elif selected_cases is not None:
            logger.warning(
                "detect_and_sync_movements: rotation batch produced 0 live cases "
                "(lawyer_id=%s) — possible ghost/stale rows",
                lawyer_id,
            )
    else:
        if selected_cases is not None:
            cap_msg = f"rotation batch of {len(cases_for_check)} cases"
        elif rol:
            cap_msg = f"rol={rol}"
        else:
            cap_msg = f"first {len(cases_for_check)} of {len(api_cases)} cases"
        logger.info("detect_and_sync_movements: fetching movements for %s", cap_msg)

    # Coverage / progress log — emitted once per batch so operators can gauge
    # rotation throughput and spot starvation before it becomes a problem.
    logger.info(
        "detect_and_sync_movements: batch=%d / total_api=%d; oldest_unchecked=%s",
        len(cases_for_check),
        len(api_cases),
        _oldest_unchecked_label(db, lawyer_id),
    )

    sync_svc = SyncService(db)

    for api_case in cases_for_check:
        if not api_case.case_token:
            logger.debug(
                "detect_and_sync_movements: no case_token for %s, skipping",
                api_case.rol,
            )
            continue

        # Resolve DB case BEFORE the try block so the except branch can reference
        # it to advance last_detail_checked_at even on failure (rotation fairness).
        normalized_rol = api_case.rol.strip().upper()
        db_case = db.query(Case).filter(
            Case.lawyer_id == lawyer_id,
            Case.rol == normalized_rol,
        ).first()

        async def _do_fetch() -> Tuple[int, int]:
            """Fetch case detail, sync movements/entities/documents for *api_case*.

            Reads ``pjud_session`` from the enclosing scope so that reassigning
            ``pjud_session = new_session`` in the outer ``except`` block is
            immediately visible to a subsequent retry call.

            Returns:
                ``(movements_new_delta, alerts_created_delta)``
            """
            from app.scrapper.pjud.resilience.integration import resilient_call
            detail = await resilient_call(
                "detail",
                lambda: scraper.get_case_detail(session=pjud_session, case_token=api_case.case_token),
            )

            scraped_movements = convert_api_movements_to_scraped([
                {
                    "folio": m.folio,
                    "fecha": m.fecha,
                    "tipo_tramite": m.tipo_tramite,
                    "descripcion": m.descripcion,
                    "etapa": m.etapa,
                    "foja": m.foja,
                    "tiene_documento": m.tiene_documento,
                }
                for m in detail.movements
            ])

            if not db_case:
                logger.warning(
                    "detect_and_sync_movements: DB case not found for "
                    "rol=%s lawyer=%s; movements and entity sync skipped",
                    api_case.rol,
                    lawyer_id,
                )
                return 0, 0

            # Hoist lawyer + webhooks once (used by both movements and entities).
            case_lawyer = db.query(Lawyer).filter(
                Lawyer.id == db_case.lawyer_id
            ).first()
            case_webhooks: list = []
            if case_lawyer:
                case_webhooks = db.query(Webhook).filter(
                    Webhook.lawyer_id == case_lawyer.id,
                    Webhook.is_active == True,  # noqa: E712
                ).all()

            # Shared budget across movements + all entity types (ADR-005).
            shared_budget = NotifyBudget.from_settings()
            notification_svc = NotificationService(db)

            new_count = 0
            alert_count = 0

            # 1. Sync movements (with shared budget).
            if scraped_movements:
                new_count, alert_count = sync_svc.sync_movements(
                    case_id=int(db_case.id),
                    scraped_movements=scraped_movements,
                    budget=shared_budget,
                )
                logger.info(
                    "detect_and_sync_movements: %s → %d new movements, %d alerts",
                    api_case.rol,
                    new_count,
                    alert_count,
                )

            # 2. Sync entity types in priority order (S1-T12 + S2-T03).
            # Litigantes are stored but SILENT (ADR-004); the rest alert + notify.
            for entity_list, spec in [
                (detail.litigantes, SPEC_LITIGANTE),
                (detail.notificaciones, SPEC_NOTIFICACION),
                (detail.escritos, SPEC_ESCRITO),
                (detail.exhortos, SPEC_EXHORTO),
            ]:
                _sync_entities(
                    db,
                    int(db_case.id),
                    entity_list,
                    spec,
                    case=db_case,
                    lawyer=case_lawyer,
                    webhooks=case_webhooks,
                    notification_svc=notification_svc,
                    budget=shared_budget,
                )

            # 3. Persist document tokens (Slice 1 — S1-T13).
            # Must run after sync_movements so that Movement rows exist for
            # the folio-based lookup inside persist_from_detail.
            persisted_docs = DocumentPersistenceService().persist_from_detail(
                detail, int(db_case.id), db
            )

            # Mark this case as detail-checked so the rotation scheduler
            # advances it to the back of the queue for the next run.
            # Runs even on 0-movement fetches — guarantees full-cycle coverage.
            db_case.last_detail_checked_at = datetime.utcnow()

            # Recompute procedural deadlines (civil only; flush-only; never raises).
            # Shares this call's NotifyBudget (ADR-005) so a ROJO-entry/fatal
            # alert drains the same per-sync-cycle pool as movements/entities.
            _maybe_recompute_deadlines(db, db_case, budget=shared_budget)

            # Commit entity upserts + document tokens + mark-checked + deadlines.
            # (sync_movements already committed its own changes.)
            db.commit()

            # 4. Synchronous document download (Slice 2 — S2-T6).
            # MUST run in this same sync task while the live browser page and
            # freshly-parsed JWT tokens are still valid (1-hour expiry window).
            if settings.DOC_DOWNLOAD_ENABLED and persisted_docs:
                from app.services.document_downloader import (
                    DocumentDownloader,
                    AsyncSleepLimiter,
                )
                from app.services.storage_service import StorageService, get_storage_backend

                # Include "failed" docs so transient download failures are retried
                # on the next scheduled run.
                pending_docs = [
                    d for d in persisted_docs if d.status in ("pending", "failed")
                ]
                if pending_docs:
                    storage_svc = StorageService(get_storage_backend(settings))
                    await DocumentDownloader().download_and_store(
                        pending_docs=pending_docs,
                        scraper=scraper,
                        pjud_session=pjud_session,
                        db=db,
                        storage_service=storage_svc,
                        limiter=AsyncSleepLimiter(delay=settings.DOCUMENT_INTER_DELAY),
                        enabled=True,
                    )

            return new_count, alert_count

        try:
            delta_m, delta_a = await _do_fetch()
            movements_new += delta_m
            alerts_created += delta_a
            shape_cooldown.clear()

        except ShapeChallengeError as shape_exc:
            # Shape/TSPD flags the egress IP's behavioral reputation — it is a
            # station-wide RATE/behavioral block, not a per-session expiry.
            # Reauth + immediate retry (the SessionExpiredError path below)
            # would hammer the flagged IP again during its reputation-cooldown
            # window and deepen the block. Do NOT reauth, do NOT retry: trip
            # the station-wide cooldown and abort this lawyer's batch outright.
            cooldown_seconds = shape_cooldown.trip()
            logger.error(
                "detect_and_sync_movements: Shape/TSPD challenge detected for "
                "lawyer_id=%s rol=%s — tripping station-wide cooldown for "
                "%.0fs, aborting batch: %s",
                lawyer_id,
                api_case.rol,
                cooldown_seconds,
                shape_exc,
            )
            # Not the case's fault — preserve rotation position, same as a
            # genuine session error.
            errors.append(
                f"Shape/TSPD challenge detected processing {api_case.rol}; "
                f"station-wide cooldown tripped for {cooldown_seconds:.0f}s; "
                "batch stopped"
            )
            break

        except (SessionExpiredError, SessionNotAuthenticatedError) as session_exc:
            logger.warning(
                "detect_and_sync_movements: session error for lawyer_id=%s rol=%s: %s",
                lawyer_id,
                api_case.rol,
                session_exc,
            )
            # Session failure is NOT the case's fault.  Do NOT advance
            # last_detail_checked_at — preserve the rotation position so this
            # case is retried on the next batch with a fresh session.
            new_session = None
            if reauth_callback is not None:
                try:
                    new_session = await reauth_callback()
                except Exception as reauth_exc:
                    logger.error(
                        "detect_and_sync_movements: reauth failed: %s", reauth_exc
                    )

            if new_session is None:
                logger.warning(
                    "detect_and_sync_movements: stopping batch — "
                    "session expired and no reauth available (lawyer_id=%s, rol=%s)",
                    lawyer_id,
                    api_case.rol,
                )
                errors.append(
                    f"Session expired processing {api_case.rol}; batch stopped"
                )
                break

            # Reauth succeeded — update the local session reference and retry once.
            pjud_session = new_session
            try:
                delta_m, delta_a = await _do_fetch()
                movements_new += delta_m
                alerts_created += delta_a
                shape_cooldown.clear()
            except ShapeChallengeError as shape_retry_exc:
                # The retry (after a successful reauth) itself hit a Shape
                # challenge — same treatment as the first-attempt case: no
                # further retry, trip the cooldown, abort.
                cooldown_seconds = shape_cooldown.trip()
                logger.error(
                    "detect_and_sync_movements: Shape/TSPD challenge on retry "
                    "for lawyer_id=%s rol=%s — tripping station-wide cooldown "
                    "for %.0fs, aborting batch: %s",
                    lawyer_id,
                    api_case.rol,
                    cooldown_seconds,
                    shape_retry_exc,
                )
                errors.append(
                    f"Shape/TSPD challenge detected on retry for {api_case.rol}; "
                    f"station-wide cooldown tripped for {cooldown_seconds:.0f}s; "
                    "batch stopped"
                )
                break
            except (SessionExpiredError, SessionNotAuthenticatedError):
                logger.error(
                    "detect_and_sync_movements: second session expiry after reauth; "
                    "stopping batch (lawyer_id=%s, rol=%s)",
                    lawyer_id,
                    api_case.rol,
                )
                errors.append(
                    f"Session expired again on retry for {api_case.rol}; batch stopped"
                )
                break
            except Exception as retry_exc:
                # Non-session error on retry — case-specific; advance timestamp.
                db.rollback()
                if db_case is not None:
                    db_case.last_detail_checked_at = datetime.utcnow()
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                logger.error(
                    "detect_and_sync_movements: retry failed for %s: %s",
                    api_case.rol,
                    retry_exc,
                )
                errors.append(
                    f"Movement fetch failed on retry for {api_case.rol}: {retry_exc}"
                )

        except Exception as exc:
            # Rollback any partial entity/alert rows flushed (but not yet committed)
            # during the try block.  Without this, the except's db.commit() would
            # silently persist half-written entity state alongside the mark-checked
            # UPDATE.  sync_movements already committed earlier — unaffected.
            db.rollback()
            # Infrastructure errors (e.g. the Mis Causas panel got torn down by a
            # prior detail navigation) are NOT case-specific — the case has real
            # detail we simply couldn't reach. Do NOT advance last_detail_checked_at
            # for these, so the case is retried instead of being falsely marked done.
            is_infra_error = any(
                marker in str(exc).lower()
                for marker in ("panel not loaded", "modal content empty")
            )
            if db_case is not None and not is_infra_error:
                # Case-specific error (detail parse, not-found, etc.). Advance so a
                # persistently-failing case rotates to the back and doesn't block
                # the rotation batch on every scheduled run.
                db_case.last_detail_checked_at = datetime.utcnow()
                try:
                    db.commit()
                except Exception:
                    db.rollback()
            logger.error(
                "detect_and_sync_movements: failed to fetch/process for %s: %s",
                api_case.rol,
                exc,
            )
            errors.append(f"Movement fetch failed for {api_case.rol}: {str(exc)}")

        if delay_between_fetches > 0:
            # ±40% jitter so the cadence isn't machine-regular (anti-fingerprint).
            await asyncio.sleep(delay_between_fetches * random.uniform(0.6, 1.4))

    logger.info(
        "detect_and_sync_movements: done — %d new movements, %d alerts, %d errors",
        movements_new,
        alerts_created,
        len(errors),
    )
    return movements_new, alerts_created, errors


async def sync_via_consulta(
    db: Session,
    lawyer: Lawyer,
    scraper,
    session,
    cases: list,
    *,
    dry_run: bool = False,
    reauth_callback: Optional[Callable[[], Awaitable[Optional[Any]]]] = None,
) -> Tuple[int, int, int, int]:
    """Persist movements for cases sourced from PJUD Consulta Unificada by rol.

    For each Case ORM object in ``cases``:

    - If ``consulta_by_rol`` returns ``None``: the case is reserved (not visible
      in the public consulta); sets ``case.consulta_reserved = True`` so the
      runner can route it to the Mis-Causas rotation. Does NOT advance
      ``last_detail_checked_at``.
    - If a ``PJUDCaseDetail`` is returned: persists movements, entities, and
      documents through the same pipeline used by ``detect_and_sync_movements``;
      clears ``case.consulta_reserved = False`` and advances
      ``last_detail_checked_at``.
    - If the court has no ``pjud_corte``: skips with a warning and counts as
      error. No crash.

    When ``dry_run=True``, no DB mutations or document downloads are performed.

    Returns:
        Tuple of (created_movements, alerts_created, reserved, errors).
    """
    created_movements: int = 0
    alerts_created: int = 0
    reserved: int = 0
    errors: int = 0

    sync_svc = SyncService(db)
    webhooks: list = (
        db.query(Webhook)
        .filter(Webhook.lawyer_id == lawyer.id, Webhook.is_active == True)
        .all()
    )
    notification_svc = NotificationService(db)

    for case in cases:
        try:
            corte = case.court.pjud_corte
            if corte is None:
                logger.warning(
                    "sync_via_consulta: case_id=%s rol=%s — court_id=%s has no "
                    "pjud_corte; skipping",
                    case.id, case.rol, case.court_id,
                )
                errors += 1
                continue

            try:
                detail = await scraper.consulta_by_rol(session, case.rol, str(corte))
            except ConsultaSessionExpired:
                if reauth_callback is not None:
                    new_session = await reauth_callback()
                    if new_session is None:
                        logger.error(
                            "sync_via_consulta: reauth returned None for case %s, skipping",
                            case.rol,
                        )
                        errors += 1
                        continue
                    session = new_session
                    try:
                        detail = await scraper.consulta_by_rol(session, case.rol, str(corte))
                    except ConsultaSessionExpired:
                        logger.error(
                            "sync_via_consulta: session expired again after reauth for case %s",
                            case.rol,
                        )
                        errors += 1
                        continue
                    except Exception as exc:
                        logger.error(
                            "sync_via_consulta: error on retry for case %s: %s",
                            case.rol, exc,
                        )
                        errors += 1
                        continue
                else:
                    logger.error(
                        "sync_via_consulta: ConsultaSessionExpired for case %s, "
                        "no reauth_callback provided",
                        case.rol,
                    )
                    errors += 1
                    continue

            if detail is None:
                logger.info(
                    "sync_via_consulta: case_id=%s rol=%s is reserved "
                    "(not found in consulta)",
                    case.id, case.rol,
                )
                if not dry_run:
                    case.consulta_reserved = True
                    db.commit()
                reserved += 1
                continue

            # Public case — persist via the existing movement + document pipeline.
            scraped_movements = convert_api_movements_to_scraped([
                {
                    "folio": m.folio,
                    "fecha": m.fecha,
                    "tipo_tramite": m.tipo_tramite,
                    "descripcion": m.descripcion,
                    "etapa": m.etapa,
                    "foja": m.foja,
                    "tiene_documento": m.tiene_documento,
                }
                for m in detail.movements
            ])

            if not dry_run:
                shared_budget = NotifyBudget.from_settings()

                if scraped_movements:
                    new_count, alert_count = sync_svc.sync_movements(
                        case_id=int(case.id),
                        scraped_movements=scraped_movements,
                        budget=shared_budget,
                    )
                    created_movements += new_count
                    alerts_created += alert_count
                    logger.info(
                        "sync_via_consulta: %s → %d new movements, %d alerts",
                        case.rol, new_count, alert_count,
                    )

                for entity_list, spec in [
                    (detail.litigantes, SPEC_LITIGANTE),
                    (detail.notificaciones, SPEC_NOTIFICACION),
                    (detail.escritos, SPEC_ESCRITO),
                    (detail.exhortos, SPEC_EXHORTO),
                ]:
                    _sync_entities(
                        db, int(case.id), entity_list, spec,
                        case=case, lawyer=lawyer, webhooks=webhooks,
                        notification_svc=notification_svc, budget=shared_budget,
                    )

                persisted_docs = DocumentPersistenceService().persist_from_detail(
                    detail, int(case.id), db
                )

                case.consulta_reserved = False
                # Only advance the detail-rotation cursor when the consulta actually
                # returned movements. A 0-movement consulta means the cuaderno is NOT
                # public (the movimientos are only visible via Mis Causas / the owner's
                # session). Leaving last_detail_checked_at untouched keeps the case at
                # the front of the detail-rotation queue (NULLS FIRST) so the owner-side
                # rotation brings the real movements — instead of being wrongly marked
                # "checked" with an empty cuaderno and stuck as 'indeterminate' forever.
                if detail.movements:
                    case.last_detail_checked_at = datetime.utcnow()
                # Shares this call's NotifyBudget (ADR-005) — see
                # detect_and_sync_movements for the same rationale.
                _maybe_recompute_deadlines(db, case, budget=shared_budget)
                db.commit()

                if settings.DOC_DOWNLOAD_ENABLED and persisted_docs:
                    from app.services.document_downloader import (
                        DocumentDownloader,
                        AsyncSleepLimiter,
                    )
                    from app.services.storage_service import (
                        StorageService,
                        get_storage_backend,
                    )
                    pending_docs = [
                        d for d in persisted_docs if d.status in ("pending", "failed")
                    ]
                    if pending_docs:
                        storage_svc = StorageService(get_storage_backend(settings))
                        await DocumentDownloader().download_and_store(
                            pending_docs=pending_docs,
                            scraper=scraper,
                            pjud_session=session,
                            db=db,
                            storage_service=storage_svc,
                            limiter=AsyncSleepLimiter(delay=settings.DOCUMENT_INTER_DELAY),
                            enabled=True,
                        )
            else:
                logger.info(
                    "sync_via_consulta [dry_run]: case_id=%s rol=%s → %d movements "
                    "in detail (not persisted)",
                    case.id, case.rol, len(scraped_movements),
                )

        except Exception as exc:
            db.rollback()
            logger.error(
                "sync_via_consulta: failed for case_id=%s rol=%s: %s",
                getattr(case, "id", "?"), getattr(case, "rol", "?"), exc,
            )
            errors += 1
            continue

    logger.info(
        "sync_via_consulta: done — %d new movements, %d alerts, %d reserved, %d errors",
        created_movements, alerts_created, reserved, errors,
    )
    return created_movements, alerts_created, reserved, errors
