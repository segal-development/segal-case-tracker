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
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple, Awaitable, TypeVar
from sqlalchemy.orm import Session
from sqlalchemy import and_

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
    """Configuration for one entity type in the generic _sync_entities engine.

    Slice 1b: creates_alert and notify are False for all four specs.
    Slice 2 will flip those flags and wire Alert + NotificationService.
    """

    model: type                          # SQLAlchemy model class
    entity_type: str                     # "litigante", "notificacion", etc.
    natural_key_fn: Callable[[Any], str] # (pjud_item) -> 64-char hex key
    to_model_fields: Callable[[Any, int], dict]  # (pjud_item, case_id) -> field dict
    creates_alert: bool = False
    notify: bool = False
    updatable_fields: List[str] = field(default_factory=list)
    # Fields NOT in the natural_key that should be refreshed on the existing row.


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
    creates_alert=False,
    notify=False,
    updatable_fields=["estado_notif", "obs_fallida"],
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
    creates_alert=False,
    notify=False,
    updatable_fields=["doc_token"],
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
    creates_alert=False,
    notify=False,
    updatable_fields=["estado"],
)


# ---------------------------------------------------------------------------
# Generic _sync_entities engine
# ---------------------------------------------------------------------------

def _sync_entities(
    db: Session,
    case_id: int,
    scraped_list: list,
    spec: EntitySyncSpec,
) -> list:
    """Upsert a list of scraped PJUD entity objects for a given case.

    Performs a SELECT-then-INSERT per item keyed on (case_id, natural_key).
    Existing rows have their spec.updatable_fields refreshed from the incoming
    item.  Idempotent when data is unchanged.

    DOES NOT COMMIT — callers are responsible for the surrounding transaction.
    Use sync_case_detail to persist all four entity types atomically.

    Slice 1b: creates_alert and notify are False on all specs — no Alert rows
    are created, NotificationService is never called.
    Slice 2 will extend this function to wire alerts and dispatch.

    Returns:
        List of (row, is_new) tuples — one per input item.
        is_new=True only for genuine inserts; an updatable-field refresh on an
        existing row yields is_new=False.
    """
    results: list = []
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

        fields = spec.to_model_fields(item, case_id)
        fields["natural_key"] = key
        fields["created_at"] = datetime.utcnow()
        row = spec.model(**fields)
        db.add(row)
        db.flush()  # assign PK within the outer transaction
        results.append((row, True))

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
        
        for scraped in scraped_cases:
            try:
                case, is_new = self._upsert_case(lawyer_id, scraped, competencia)
                if is_new:
                    result.cases_new += 1
                else:
                    result.cases_updated += 1
            except Exception as e:
                result.errors.append(f"Error syncing {scraped.rol}: {str(e)}")
        
        # Update sync record
        sync_record.cases_found = result.cases_total
        sync_record.cases_new = result.cases_new
        sync_record.cases_updated = result.cases_updated
        sync_record.complete(
            status="completed" if not result.errors else "partial",
            error="; ".join(result.errors) if result.errors else None,
        )
        
        self.db.commit()
        return result
    
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
    ) -> Tuple[int, int]:
        """
        Sync movements for a case, detecting new ones.

        Args:
            case_id: Database case ID
            scraped_movements: Movements from PJUD scraper

        Returns:
            Tuple of (new_count, alert_count)
        """
        new_count = 0
        alert_count = 0
        notify_dispatched = 0

        case = self.db.query(Case).filter(Case.id == case_id).first()
        if not case:
            return 0, 0

        # FIX 2: hoist Lawyer + active-Webhook queries outside the per-movement
        # loop — they are constant for the whole call so querying them once
        # removes O(N) round-trips and reduces the blocking-I/O window.
        lawyer = self.db.query(Lawyer).filter(
            Lawyer.id == case.lawyer_id
        ).first()
        webhooks: list = []
        if lawyer:
            webhooks = self.db.query(Webhook).filter(
                Webhook.lawyer_id == lawyer.id,
                Webhook.is_active == True,  # noqa: E712
            ).all()

        notify_max = settings.NOTIFY_MAX_PER_SYNC

        for scraped in scraped_movements:
            movement, is_new = self._upsert_movement(case_id, scraped)
            if is_new:
                new_count += 1
                # Create alert for new movement — alert is always persisted.
                alert = self._create_movement_alert(case, movement)
                if alert:
                    alert_count += 1
                    # FIX 1: cap notifications per sync call; alerts are still
                    # created above — only the dispatch is gated here.
                    if lawyer and notify_dispatched < notify_max:
                        try:
                            NotificationService(self.db).notify_new_movement(
                                alert, case, movement, lawyer, webhooks
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed to dispatch notification for alert on case %s "
                                "movement %s: %s",
                                case.id,
                                movement.id,
                                exc,
                            )
                        # Count every attempt (success or error) toward the cap.
                        notify_dispatched += 1

        # FIX 1: emit a clear warning when the cap prevented some dispatches.
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
                notify_max,
            )

        # Update last_movement_at on case
        if scraped_movements:
            latest_date = self._parse_date(scraped_movements[0].fecha)
            if latest_date and (not case.last_movement_at or latest_date > case.last_movement_at):
                case.last_movement_at = latest_date

        self.db.commit()
        return new_count, alert_count
    
    def _upsert_case(
        self,
        lawyer_id: int,
        scraped: ScrapedCase,
        competencia: str,
    ) -> Tuple[Case, bool]:
        """
        Insert or update a case.
        
        Returns:
            Tuple of (case, is_new)
        """
        # Find existing case by ROL and lawyer
        existing = self.db.query(Case).filter(
            and_(
                Case.lawyer_id == lawyer_id,
                Case.rol == scraped.rol,
            )
        ).first()
        
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
                rol=scraped.rol,
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
    
    def _create_movement_alert(self, case: Case, movement: Movement) -> Optional[Alert]:
        """Create an alert for a new movement."""
        alert = Alert(
            lawyer_id=case.lawyer_id,
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
        """Get or create a court by name."""
        # Normalize name
        name = tribunal_name.strip()
        
        existing = self.db.query(Court).filter(Court.name == name).first()
        if existing:
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
        
        code = f"{number}{initials}"
        
        # If code is too short, add hash
        if len(code) < 3:
            code = hashlib.md5(name.encode()).hexdigest()[:6].upper()
        
        return code
    
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


async def detect_and_sync_movements(
    db: Session,
    scraper,
    pjud_session,
    lawyer_id: int,
    api_cases: list,
    rol: Optional[str] = None,
    delay_between_fetches: float = 0.0,
) -> Tuple[int, int, List[str]]:
    """Fetch case details for selected cases and sync new movements to the database.

    This is the canonical movement-detection implementation shared by the
    ``POST /sync`` endpoint (on-demand) and the scheduled worker (autonomous).
    Keeping a single implementation ensures one code path to test and maintain.

    Selection is governed by ``_select_cases_for_movement_check``:
    - If *rol* is given, only that case is fetched (targeted/demo mode).
    - Otherwise, at most ``MOVEMENT_CHECK_DEFAULT_MAX`` cases from the front of
      the list are fetched (rate-limit-friendly default).

    Notifications are dispatched automatically by ``SyncService.sync_movements``
    via the existing ``NotificationService`` path (email + HMAC webhooks).

    Args:
        db: Active SQLAlchemy session.
        scraper: Scraper instance with ``get_case_detail(session, case_token)``.
        pjud_session: Active ``PJUDSession`` used for authenticated scraping.
        lawyer_id: Owner of the cases being checked.
        api_cases: List of PJUDCase objects returned by ``get_my_cases``.
        rol: Optional ROL filter — when set, only that case is detail-fetched.
        delay_between_fetches: Seconds to sleep between consecutive detail
            fetches.  Use ``0.0`` (default) for on-demand endpoint calls; use
            a small positive value (e.g. ``1.0``) in scheduled workers to be
            considerate toward the PJUD infrastructure.

    Returns:
        Tuple of ``(movements_new, alerts_created, errors)`` where *errors* is
        a list of human-readable strings for any case whose fetch or processing
        failed (the function never raises — errors are accumulated instead).
    """
    movements_new: int = 0
    alerts_created: int = 0
    errors: List[str] = []

    cases_for_check = _select_cases_for_movement_check(api_cases, rol=rol)

    if not cases_for_check:
        if rol:
            logger.warning(
                "detect_and_sync_movements: rol=%s not found in results; "
                "skipping movement detection",
                rol,
            )
    else:
        cap_msg = (
            f"rol={rol}"
            if rol
            else f"first {len(cases_for_check)} of {len(api_cases)} cases"
        )
        logger.info("detect_and_sync_movements: fetching movements for %s", cap_msg)

    sync_svc = SyncService(db)

    for api_case in cases_for_check:
        if not api_case.case_token:
            logger.debug(
                "detect_and_sync_movements: no case_token for %s, skipping",
                api_case.rol,
            )
            continue

        try:
            detail = await scraper.get_case_detail(
                session=pjud_session,
                case_token=api_case.case_token,
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

            normalized_rol = api_case.rol.strip().upper()
            db_case = db.query(Case).filter(
                Case.lawyer_id == lawyer_id,
                Case.rol == normalized_rol,
            ).first()

            if db_case and scraped_movements:
                new_count, alert_count = sync_svc.sync_movements(
                    case_id=int(db_case.id),
                    scraped_movements=scraped_movements,
                )
                movements_new += new_count
                alerts_created += alert_count
                logger.info(
                    "detect_and_sync_movements: %s → %d new movements, %d alerts",
                    api_case.rol,
                    new_count,
                    alert_count,
                )
            elif not db_case:
                logger.warning(
                    "detect_and_sync_movements: DB case not found for rol=%s lawyer=%s; "
                    "movements skipped",
                    api_case.rol,
                    lawyer_id,
                )

        except Exception as exc:
            logger.error(
                "detect_and_sync_movements: failed to fetch/process movements for %s: %s",
                api_case.rol,
                exc,
            )
            errors.append(f"Movement fetch failed for {api_case.rol}: {str(exc)}")

        if delay_between_fetches > 0:
            await asyncio.sleep(delay_between_fetches)

    return movements_new, alerts_created, errors
