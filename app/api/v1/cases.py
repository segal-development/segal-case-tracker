"""Cases endpoints - Read from database."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.api.deps import get_db, get_current_lawyer
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.movement import Movement
from app.models.court import Court
from app.models.sync_history import SyncHistory
from app.services.case_service import CaseService

router = APIRouter()


# ============================================================================
# RESPONSE SCHEMAS (inline for clarity)
# ============================================================================

from pydantic import BaseModel
from datetime import datetime


class CourtInfo(BaseModel):
    id: int
    name: str
    region: Optional[str] = None

    class Config:
        from_attributes = True


class CaseResponse(BaseModel):
    """Case from database."""
    id: int
    rol: str
    competencia: str
    court: Optional[CourtInfo] = None
    plaintiff: Optional[str] = None
    defendant: Optional[str] = None
    procedure: Optional[str] = None
    status: str
    filed_at: Optional[datetime] = None
    last_movement_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MovementResponse(BaseModel):
    """Movement from database."""
    id: int
    folio: Optional[str] = None
    stage: Optional[str] = None
    procedure: Optional[str] = None
    description: str
    movement_date: datetime
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CaseListResponse(BaseModel):
    """Paginated case list."""
    items: List[CaseResponse]
    total: int
    page: int
    per_page: int
    pages: int
    last_sync: Optional[datetime] = None


class LitiganteResponse(BaseModel):
    """Party (litigante) for a case."""
    participante: str
    rut: str
    persona_type: str
    nombre: str

    class Config:
        from_attributes = True


class NotificacionResponse(BaseModel):
    """Notification record for a case."""
    rol: str
    estado_notif: str
    tipo_notif: str
    fecha_tramite: Optional[datetime] = None
    tipo_participante: str
    nombre: str
    tramite: str
    obs_fallida: Optional[str] = None

    class Config:
        from_attributes = True


class EscritoResponse(BaseModel):
    """Filing (escrito) record for a case."""
    fecha_ingreso: Optional[datetime] = None
    tipo_escrito: str
    solicitante: str
    tiene_documento: bool
    tiene_anexo: bool
    doc_token: Optional[str] = None

    class Config:
        from_attributes = True


class ExhortoResponse(BaseModel):
    """Exhorto/rogatory letter record for a case."""
    rol_origen: str
    tipo_exhorto: str
    rol_destino: str
    fecha_ordena: Optional[datetime] = None
    fecha_ingreso: Optional[datetime] = None
    tribunal_destino: str
    estado: str

    class Config:
        from_attributes = True


class CaseEntitiesResponse(BaseModel):
    """Lightweight response containing only the 4 entity lists for a case."""
    litigantes: List[LitiganteResponse]
    notificaciones: List[NotificacionResponse]
    escritos: List[EscritoResponse]
    exhortos: List[ExhortoResponse]


class CaseDetailResponse(BaseModel):
    """Case with movements and all scraped entity lists."""
    case: CaseResponse
    movements: List[MovementResponse]
    movements_count: int
    litigantes: List[LitiganteResponse]
    litigantes_count: int
    notificaciones: List[NotificacionResponse]
    notificaciones_count: int
    escritos: List[EscritoResponse]
    escritos_count: int
    exhortos: List[ExhortoResponse]
    exhortos_count: int


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.get("", response_model=CaseListResponse)
async def list_cases(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    competencia: Optional[str] = Query(None, description="Filter by competencia: civil, laboral, penal"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: active, closed"),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    List all cases for the authenticated lawyer.
    
    Returns cases from database (fast). Use /sync to refresh from PJUD.
    """
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not lawyer_id:
        raise HTTPException(status_code=401, detail="Invalid token: no lawyer_id")
    
    # Build query
    query = db.query(Case).filter(Case.lawyer_id == lawyer_id)
    
    if competencia:
        query = query.filter(Case.competencia == competencia)
    if status_filter:
        query = query.filter(Case.status == status_filter)
    
    # Get total count
    total = query.count()
    
    # Paginate
    cases = query.order_by(Case.updated_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    
    # Get last sync time
    last_sync_record = db.query(SyncHistory).filter(
        and_(
            SyncHistory.lawyer_id == lawyer_id,
            SyncHistory.status.in_(["completed", "partial"]),
        )
    ).order_by(SyncHistory.completed_at.desc()).first()
    
    last_sync = last_sync_record.completed_at if last_sync_record else None
    
    # Calculate pages
    pages = (total + per_page - 1) // per_page if total > 0 else 0
    
    # Build response with court info
    items = []
    for case in cases:
        court_info = None
        if case.court:
            court_info = CourtInfo(
                id=case.court.id,
                name=case.court.name,
                region=case.court.region,
            )
        
        items.append(CaseResponse(
            id=case.id,
            rol=case.rol,
            competencia=case.competencia or "civil",
            court=court_info,
            plaintiff=case.plaintiff,
            defendant=case.defendant,
            procedure=case.procedure,
            status=case.status,
            filed_at=case.filed_at,
            last_movement_at=case.last_movement_at,
            created_at=case.created_at,
            updated_at=case.updated_at,
        ))
    
    return CaseListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
        last_sync=last_sync,
    )


@router.get("/{case_id}", response_model=CaseDetailResponse)
async def get_case(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get a specific case with its movements."""
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    
    case = db.query(Case).filter(
        and_(
            Case.id == case_id,
            Case.lawyer_id == lawyer_id,
        )
    ).first()
    
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )
    
    # Get movements
    movements = db.query(Movement).filter(
        Movement.case_id == case_id
    ).order_by(Movement.movement_date.desc()).all()

    # Get case-detail entities
    litigantes = db.query(CaseLitigante).filter(
        CaseLitigante.case_id == case_id
    ).all()
    notificaciones = db.query(CaseNotificacion).filter(
        CaseNotificacion.case_id == case_id
    ).all()
    escritos = db.query(CaseEscrito).filter(
        CaseEscrito.case_id == case_id
    ).all()
    exhortos = db.query(CaseExhorto).filter(
        CaseExhorto.case_id == case_id
    ).all()

    # Build court info
    court_info = None
    if case.court:
        court_info = CourtInfo(
            id=case.court.id,
            name=case.court.name,
            region=case.court.region,
        )

    case_response = CaseResponse(
        id=case.id,
        rol=case.rol,
        competencia=case.competencia or "civil",
        court=court_info,
        plaintiff=case.plaintiff,
        defendant=case.defendant,
        procedure=case.procedure,
        status=case.status,
        filed_at=case.filed_at,
        last_movement_at=case.last_movement_at,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )

    movements_response = [
        MovementResponse(
            id=m.id,
            folio=m.folio,
            stage=m.stage,
            procedure=m.procedure,
            description=m.description,
            movement_date=m.movement_date,
            created_at=m.created_at,
        )
        for m in movements
    ]

    return CaseDetailResponse(
        case=case_response,
        movements=movements_response,
        movements_count=len(movements),
        litigantes=[LitiganteResponse.model_validate(r) for r in litigantes],
        litigantes_count=len(litigantes),
        notificaciones=[NotificacionResponse.model_validate(r) for r in notificaciones],
        notificaciones_count=len(notificaciones),
        escritos=[EscritoResponse.model_validate(r) for r in escritos],
        escritos_count=len(escritos),
        exhortos=[ExhortoResponse.model_validate(r) for r in exhortos],
        exhortos_count=len(exhortos),
    )


@router.get("/{case_id}/movements", response_model=List[MovementResponse])
async def get_case_movements(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get all movements for a case."""
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    
    # Verify case belongs to lawyer
    case = db.query(Case).filter(
        and_(
            Case.id == case_id,
            Case.lawyer_id == lawyer_id,
        )
    ).first()
    
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )
    
    movements = db.query(Movement).filter(
        Movement.case_id == case_id
    ).order_by(Movement.movement_date.desc()).all()
    
    return [
        MovementResponse(
            id=m.id,
            folio=m.folio,
            stage=m.stage,
            procedure=m.procedure,
            description=m.description,
            movement_date=m.movement_date,
            created_at=m.created_at,
        )
        for m in movements
    ]


@router.get("/{case_id}/detail-entities", response_model=CaseEntitiesResponse)
async def get_case_detail_entities(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Get only the 4 entity lists for a case (litigantes, notificaciones, escritos, exhortos)."""
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")

    # Verify case belongs to lawyer
    case = db.query(Case).filter(
        and_(
            Case.id == case_id,
            Case.lawyer_id == lawyer_id,
        )
    ).first()

    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    litigantes = db.query(CaseLitigante).filter(
        CaseLitigante.case_id == case_id
    ).all()
    notificaciones = db.query(CaseNotificacion).filter(
        CaseNotificacion.case_id == case_id
    ).all()
    escritos = db.query(CaseEscrito).filter(
        CaseEscrito.case_id == case_id
    ).all()
    exhortos = db.query(CaseExhorto).filter(
        CaseExhorto.case_id == case_id
    ).all()

    return CaseEntitiesResponse(
        litigantes=[LitiganteResponse.model_validate(r) for r in litigantes],
        notificaciones=[NotificacionResponse.model_validate(r) for r in notificaciones],
        escritos=[EscritoResponse.model_validate(r) for r in escritos],
        exhortos=[ExhortoResponse.model_validate(r) for r in exhortos],
    )


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_case(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Archive a case (soft delete)."""
    lawyer_id = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    
    case = db.query(Case).filter(
        and_(
            Case.id == case_id,
            Case.lawyer_id == lawyer_id,
        )
    ).first()
    
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )
    
    case.status = "archived"
    db.commit()
