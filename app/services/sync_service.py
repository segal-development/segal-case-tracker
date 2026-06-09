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
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple, Callable, Awaitable, TypeVar
from sqlalchemy.orm import Session
from sqlalchemy import and_

logger = logging.getLogger(__name__)

# Maximum concurrent case detail fetches
MAX_CONCURRENT_FETCHES = 3

T = TypeVar("T")

from app.models.case import Case
from app.models.movement import Movement
from app.models.court import Court
from app.models.alert import Alert
from app.models.lawyer import Lawyer
from app.models.sync_history import SyncHistory


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
        
        case = self.db.query(Case).filter(Case.id == case_id).first()
        if not case:
            return 0, 0
        
        for scraped in scraped_movements:
            movement, is_new = self._upsert_movement(case_id, scraped)
            if is_new:
                new_count += 1
                # Create alert for new movement
                alert = self._create_movement_alert(case, movement)
                if alert:
                    alert_count += 1
        
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
