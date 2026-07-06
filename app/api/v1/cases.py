"""Cases endpoints - Read from database."""

from typing import List, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import nullslast

from app.api.deps import (
    get_db,
    get_current_lawyer,
    _resolve_lawyer_id,
    resolve_case_scope,
    apply_case_scope,
    ALL_CASES,
)
from app.models.case import Case
from app.models.lawyer import Lawyer
from app.models.case_litigante import CaseLitigante
from app.models.case_notificacion import CaseNotificacion
from app.models.case_escrito import CaseEscrito
from app.models.case_exhorto import CaseExhorto
from app.models.movement import Movement
from app.models.court import Court
from app.models.sync_history import SyncHistory
from app.models.document import Document

router = APIRouter()


# ============================================================================
# RESPONSE SCHEMAS (inline for clarity)
# ============================================================================

from pydantic import BaseModel
from datetime import date, datetime


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
    # Procedural deadline engine fields (populated by DeadlineEngine.recompute_case)
    procedural_state: Optional[str] = None
    semaforo: Optional[str] = None
    next_deadline_at: Optional[date] = None
    abandono_disponible: bool = False
    next_deadline_fatal: bool = False
    en_apremio: bool = False
    # Prescripción advisory
    titulo_tipo: Optional[str] = None
    titulo_fecha: Optional[date] = None
    prescripcion_cumplida: bool = False
    prescripcion_fecha: Optional[date] = None

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


class DocumentListItem(BaseModel):
    """Single document row returned by the list endpoint."""
    id: int
    doc_type: Optional[str] = None
    pjud_endpoint: Optional[str] = None
    filename: Optional[str] = None
    status: str
    available: bool
    download_url: str
    # Filing date in the expediente = the linked movement's date. None for
    # case-level static docs (texto_demanda, cert_envio, ebook) which have no
    # movement and no date in PJUD's data. (Named doc_date, not date, to avoid
    # shadowing the imported `date` type in the annotation.)
    doc_date: Optional[date] = None

    class Config:
        from_attributes = True


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
    abogado_rut: Optional[str] = Query(None, description="Filter to cases where this RUT is a firm-side abogado"),
    sort_by: Optional[Literal["criticidad", "updated_at"]] = Query(
        None,
        description=(
            "Sort order. 'criticidad' orders by next_deadline_at ASC NULLS LAST "
            "(most-urgent first). Default: updated_at DESC."
        ),
    ),
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """
    List all cases for the authenticated lawyer.

    Returns cases from database (fast). Use /sync to refresh from PJUD.
    """
    lawyer_id = _resolve_lawyer_id(db, current_lawyer)
    if not lawyer_id:
        raise HTTPException(status_code=401, detail="Invalid token: no lawyer_id")

    # Auditor scope: spans every study case (no single-lawyer filter). Every
    # other role stays scoped to its own cases via lawyer_id, same as before.
    scope = resolve_case_scope(db, current_lawyer)

    # Build query
    query = db.query(Case)
    query = apply_case_scope(query, scope)

    if competencia:
        query = query.filter(Case.competencia == competencia)
    if status_filter:
        query = query.filter(Case.status == status_filter)

    if abogado_rut:
        from app.services.lawyer_roster import case_ids_for_abogado
        account_lawyer = db.get(Lawyer, lawyer_id)
        if account_lawyer:
            allowed_ids = case_ids_for_abogado(db, account_lawyer.rut, abogado_rut)
            query = query.filter(Case.id.in_(list(allowed_ids)))

    # Get total count
    total = query.count()

    # Apply sort order
    if sort_by == "criticidad":
        # ORDER BY next_deadline_at ASC NULLS LAST — most-critical/soonest first
        order_clause = nullslast(Case.next_deadline_at.asc())
    else:
        order_clause = Case.updated_at.desc()

    # Stable secondary sort on the unique id. The primary sort keys
    # (next_deadline_at / updated_at) are non-unique, so without a deterministic
    # tiebreaker pagination is unstable: the same case can land on two pages
    # (duplicates) or fall between pages (omissions) across separate requests.
    # Paginate
    cases = (
        query.order_by(order_clause, Case.id.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    
    # Get last sync time. Auditor spans the whole firm, so surface the most
    # recent sync across ALL lawyers instead of one account's own history.
    last_sync_query = db.query(SyncHistory).filter(
        SyncHistory.status.in_(["completed", "partial"])
    )
    if scope is not ALL_CASES:
        last_sync_query = last_sync_query.filter(SyncHistory.lawyer_id == lawyer_id)
    last_sync_record = last_sync_query.order_by(SyncHistory.completed_at.desc()).first()
    
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
            procedural_state=case.procedural_state,
            semaforo=case.semaforo,
            next_deadline_at=case.next_deadline_at,
            abandono_disponible=case.abandono_disponible or False,
            next_deadline_fatal=case.next_deadline_fatal or False,
            en_apremio=case.en_apremio or False,
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
    scope = resolve_case_scope(db, current_lawyer)

    case = apply_case_scope(
        db.query(Case).filter(Case.id == case_id), scope
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
        procedural_state=case.procedural_state,
        semaforo=case.semaforo,
        next_deadline_at=case.next_deadline_at,
        abandono_disponible=case.abandono_disponible or False,
        next_deadline_fatal=case.next_deadline_fatal or False,
        en_apremio=case.en_apremio or False,
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
    scope = resolve_case_scope(db, current_lawyer)

    # Verify case is visible to the caller (own case, or any case for auditor)
    case = apply_case_scope(
        db.query(Case).filter(Case.id == case_id), scope
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
    scope = resolve_case_scope(db, current_lawyer)

    # Verify case is visible to the caller (own case, or any case for auditor)
    case = apply_case_scope(
        db.query(Case).filter(Case.id == case_id), scope
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


@router.get("/{case_id}/documents", response_model=List[DocumentListItem])
async def list_case_documents(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """List all documents for a case.

    Returns document rows with a pre-built download URL for each one.
    ``available`` is ``True`` for every status except ``"unavailable"``.
    """
    scope = resolve_case_scope(db, current_lawyer)

    case = apply_case_scope(
        db.query(Case).filter(Case.id == case_id), scope
    ).first()

    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    docs = db.query(Document).filter(Document.case_id == case_id).all()

    # Map movement_id -> filing date (movement_date) for docs linked to a
    # movement — the real "when it appeared in the expediente" date. Batched to
    # avoid N+1. Case-level static docs have no movement_id → date stays None.
    mv_ids = {doc.movement_id for doc in docs if doc.movement_id}
    mv_dates: dict[int, date] = {}
    if mv_ids:
        for mid, mdate in (
            db.query(Movement.id, Movement.movement_date)
            .filter(Movement.id.in_(mv_ids))
            .all()
        ):
            if mdate is not None:
                mv_dates[mid] = mdate.date()

    return [
        DocumentListItem(
            id=doc.id,
            doc_type=doc.doc_type,
            pjud_endpoint=doc.pjud_endpoint,
            filename=doc.filename,
            status=doc.status,
            available=(doc.status != "unavailable"),
            download_url=f"/api/v1/documents/{doc.id}/download",
            doc_date=mv_dates.get(doc.movement_id) if doc.movement_id else None,
        )
        for doc in docs
    ]


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_case(
    case_id: int,
    current_lawyer: dict = Depends(get_current_lawyer),
    db: Session = Depends(get_db),
):
    """Archive a case (soft delete).

    Approach C: Case.lawyer_id is the firm's single bookkeeping owner, so it
    no longer distinguishes which lawyer may archive a case. Authorization
    is litigante-based instead: auditor/admin (firm-wide) OR the acting
    lawyer is an abogado-of-record litigante on the case
    (``acting_lawyer_is_case_abogado``). Returns 404 (not 403) when
    unauthorized to avoid leaking case existence, matching prior behavior.
    """
    from app.services.lawyer_roster import acting_lawyer_is_case_abogado

    sub = current_lawyer.get("sub") or current_lawyer.get("lawyer_id")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if isinstance(sub, int) or (isinstance(sub, str) and sub.isdigit()):
        acting_lawyer = db.query(Lawyer).filter(Lawyer.id == int(sub)).first()
    else:
        acting_lawyer = db.query(Lawyer).filter(Lawyer.rut == sub).first()

    case = db.query(Case).filter(Case.id == case_id).first()

    if not case or not acting_lawyer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    authorized = acting_lawyer.role in {"auditor", "admin"} or acting_lawyer_is_case_abogado(
        db, case, acting_lawyer.rut
    )
    if not authorized:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    case.status = "archived"
    db.commit()
