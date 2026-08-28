"""Staff evaluation module (evaluación de procuradores/personas).

Self-administrable replica of the firm's Google Form. An admin curates the
criteria (1-5 scored lines, grouped) and the list of people who can be
evaluated; any logged-in user renders the form and submits an evaluation.

- ``/criterios``   (admin) — CRUD of the scored criteria (DELETE is soft).
- ``/evaluables``  (admin) — curated list of who can be evaluated (any role).
- ``/form``        (any)   — active criterios + evaluables to render the form.
- ``POST /``       (any)   — submit one evaluation + its per-criterio scores.
- ``/resultados``  (admin) — per-evaluable aggregates (averages ignore N/A).
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, require_admin
from app.models.evaluacion import (
    Evaluacion,
    EvaluacionCriterio,
    EvaluacionEvaluable,
    EvaluacionRespuesta,
)
from app.models.lawyer import Lawyer

logger = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class CriterioCreate(BaseModel):
    label: str
    grupo: str = "Criterios"
    orden: int = 0
    permite_na: bool = False

    @field_validator("label", "grupo")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("campo obligatorio")
        return v.strip()


class CriterioUpdate(BaseModel):
    label: Optional[str] = None
    grupo: Optional[str] = None
    orden: Optional[int] = None
    permite_na: Optional[bool] = None
    activo: Optional[bool] = None

    @field_validator("label", "grupo")
    @classmethod
    def _not_blank(cls, v):
        if v is not None and not v.strip():
            raise ValueError("campo no puede quedar vacío")
        return v.strip() if v is not None else v


class CriterioResponse(BaseModel):
    id: int
    label: str
    grupo: str
    orden: int
    permite_na: bool
    activo: bool


class EvaluableCreate(BaseModel):
    lawyer_id: int


class EvaluableResponse(BaseModel):
    id: int
    lawyer_id: int
    nombre: str
    rut: Optional[str] = None
    role: Optional[str] = None
    activo: bool


class FormCriterio(BaseModel):
    id: int
    label: str
    grupo: str
    orden: int
    permite_na: bool


class FormEvaluable(BaseModel):
    lawyer_id: int
    nombre: str


class FormResponse(BaseModel):
    criterios: List[FormCriterio]
    evaluables: List[FormEvaluable]


class RespuestaInput(BaseModel):
    criterio_id: int
    puntaje: Optional[int] = None


class EvaluacionCreate(BaseModel):
    evaluado_lawyer_id: int
    evaluador_email: str  # público: el evaluador se identifica por email (no login)
    comentarios: str  # required, non-blank (see validator)
    respuestas: List[RespuestaInput] = []

    @field_validator("evaluador_email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = (v or "").strip()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Email inválido")
        return v.lower()

    @field_validator("comentarios")
    @classmethod
    def _comment_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("El comentario es obligatorio.")
        return v


class EvaluacionReset(BaseModel):
    """Admin request to free a taken (evaluador, evaluado, mes) slot."""

    evaluado_lawyer_id: int
    evaluador_email: str
    periodo: str  # "YYYY-MM"


class EvaluacionCreated(BaseModel):
    id: int
    evaluado_lawyer_id: int
    evaluador_email: str
    comentarios: Optional[str] = None
    respuestas: List[RespuestaInput]


class ResultadoCriterio(BaseModel):
    criterio_id: int
    label: str
    grupo: str
    promedio: Optional[float] = None
    n: int


class ResultadoEvaluable(BaseModel):
    lawyer_id: int
    nombre: str
    total_evaluaciones: int
    promedio_general: Optional[float] = None
    por_criterio: List[ResultadoCriterio]
    comentarios: List[str]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _criterio_response(c: EvaluacionCriterio) -> CriterioResponse:
    return CriterioResponse(
        id=c.id,
        label=c.label,
        grupo=c.grupo,
        orden=c.orden,
        permite_na=c.permite_na,
        activo=c.activo,
    )


# --------------------------------------------------------------------------- #
# Criterios (admin)
# --------------------------------------------------------------------------- #
@router.get("/criterios", response_model=List[CriterioResponse])
async def list_criterios(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """All criteria (including inactive), ordered by (grupo, orden, id)."""
    rows = (
        db.query(EvaluacionCriterio)
        .order_by(
            EvaluacionCriterio.grupo,
            EvaluacionCriterio.orden,
            EvaluacionCriterio.id,
        )
        .all()
    )
    return [_criterio_response(c) for c in rows]


@router.post(
    "/criterios",
    response_model=CriterioResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_criterio(
    body: CriterioCreate,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Create a criterion."""
    c = EvaluacionCriterio(
        label=body.label[:255],
        grupo=body.grupo[:100],
        orden=body.orden,
        permite_na=body.permite_na,
        activo=True,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _criterio_response(c)


@router.put("/criterios/{criterio_id}", response_model=CriterioResponse)
async def update_criterio(
    criterio_id: int,
    body: CriterioUpdate,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Update a criterion (partial)."""
    c = db.query(EvaluacionCriterio).filter(EvaluacionCriterio.id == criterio_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="Criterio no encontrado")
    if body.label is not None:
        c.label = body.label[:255]
    if body.grupo is not None:
        c.grupo = body.grupo[:100]
    if body.orden is not None:
        c.orden = body.orden
    if body.permite_na is not None:
        c.permite_na = body.permite_na
    if body.activo is not None:
        c.activo = body.activo
    db.commit()
    db.refresh(c)
    return _criterio_response(c)


@router.delete("/criterios/{criterio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_criterio(
    criterio_id: int,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Soft-delete a criterion (activo=False) to preserve historical responses."""
    c = db.query(EvaluacionCriterio).filter(EvaluacionCriterio.id == criterio_id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="Criterio no encontrado")
    c.activo = False
    db.commit()


# --------------------------------------------------------------------------- #
# Evaluables (admin)
# --------------------------------------------------------------------------- #
@router.get("/evaluables", response_model=List[EvaluableResponse])
async def list_evaluables(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """The curated list of who can be evaluated (join Lawyer), active first."""
    rows = (
        db.query(EvaluacionEvaluable)
        .options(joinedload(EvaluacionEvaluable.lawyer))
        .order_by(EvaluacionEvaluable.activo.desc(), EvaluacionEvaluable.id)
        .all()
    )
    out = []
    for e in rows:
        out.append(
            EvaluableResponse(
                id=e.id,
                lawyer_id=e.lawyer_id,
                nombre=e.lawyer.name if e.lawyer else f"#{e.lawyer_id}",
                rut=e.lawyer.rut if e.lawyer else None,
                role=e.lawyer.role if e.lawyer else None,
                activo=e.activo,
            )
        )
    return out


@router.post(
    "/evaluables",
    response_model=EvaluableResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_evaluable(
    body: EvaluableCreate,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Add a lawyer as evaluable. Reactivates an existing (soft-deleted) row
    instead of duplicating."""
    lawyer = db.query(Lawyer).filter(Lawyer.id == body.lawyer_id).first()
    if lawyer is None:
        raise HTTPException(status_code=404, detail="Persona no encontrada")

    existing = (
        db.query(EvaluacionEvaluable)
        .filter(EvaluacionEvaluable.lawyer_id == body.lawyer_id)
        .first()
    )
    if existing is not None:
        existing.activo = True
        db.commit()
        db.refresh(existing)
        row = existing
    else:
        row = EvaluacionEvaluable(lawyer_id=body.lawyer_id, activo=True)
        db.add(row)
        db.commit()
        db.refresh(row)

    return EvaluableResponse(
        id=row.id,
        lawyer_id=row.lawyer_id,
        nombre=lawyer.name,
        rut=lawyer.rut,
        role=lawyer.role,
        activo=row.activo,
    )


@router.delete("/evaluables/{evaluable_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_evaluable(
    evaluable_id: int,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Soft-delete an evaluable (activo=False)."""
    e = db.query(EvaluacionEvaluable).filter(EvaluacionEvaluable.id == evaluable_id).first()
    if e is None:
        raise HTTPException(status_code=404, detail="Evaluable no encontrado")
    e.activo = False
    db.commit()


# --------------------------------------------------------------------------- #
# Formulario (any logged-in)
# --------------------------------------------------------------------------- #
@router.get("/form", response_model=FormResponse)
async def get_form(
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). Everything a client needs to render the evaluation form:
    active criteria (ordered) and active evaluables."""
    criterios = (
        db.query(EvaluacionCriterio)
        .filter(EvaluacionCriterio.activo.is_(True))
        .order_by(
            EvaluacionCriterio.grupo,
            EvaluacionCriterio.orden,
            EvaluacionCriterio.id,
        )
        .all()
    )
    evaluables = (
        db.query(EvaluacionEvaluable)
        .options(joinedload(EvaluacionEvaluable.lawyer))
        .filter(EvaluacionEvaluable.activo.is_(True))
        .order_by(EvaluacionEvaluable.id)
        .all()
    )
    return FormResponse(
        criterios=[
            FormCriterio(
                id=c.id,
                label=c.label,
                grupo=c.grupo,
                orden=c.orden,
                permite_na=c.permite_na,
            )
            for c in criterios
        ],
        evaluables=[
            FormEvaluable(
                lawyer_id=e.lawyer_id,
                nombre=e.lawyer.name if e.lawyer else f"#{e.lawyer_id}",
            )
            for e in evaluables
        ],
    )


# --------------------------------------------------------------------------- #
# Enviar evaluación (any logged-in)
# --------------------------------------------------------------------------- #
@router.post("", response_model=EvaluacionCreated, status_code=status.HTTP_201_CREATED)
async def submit_evaluacion(
    body: EvaluacionCreate,
    db: Session = Depends(get_db),
):
    """PUBLIC (no auth). Submit one evaluation from the shareable form. The
    evaluador is identified by the email they provide (``evaluador_email``).

    Validates: evaluado is an ACTIVE evaluable; every criterio_id is an active
    criterio; each puntaje is an int 1..5, or null only when the criterio has
    permite_na=True.
    """
    evaluable = (
        db.query(EvaluacionEvaluable)
        .filter(EvaluacionEvaluable.lawyer_id == body.evaluado_lawyer_id)
        .first()
    )
    if evaluable is None or not evaluable.activo:
        raise HTTPException(
            status_code=400, detail="La persona evaluada no está en la lista de evaluables"
        )

    # Monthly limit: one evaluation per (evaluador, evaluado, mes). The period is
    # derived server-side from the current month (never taken from the request).
    periodo = datetime.utcnow().strftime("%Y-%m")
    already = (
        db.query(Evaluacion)
        .filter(
            Evaluacion.evaluado_lawyer_id == body.evaluado_lawyer_id,
            func.lower(Evaluacion.evaluador_email) == body.evaluador_email,
            Evaluacion.periodo == periodo,
        )
        .first()
    )
    if already is not None:
        raise HTTPException(
            status_code=409,
            detail="Ya registraste una evaluación de esta persona este mes.",
        )

    # Load the active criteria referenced, keyed by id for validation.
    criterio_ids = [r.criterio_id for r in body.respuestas]
    criterios = {
        c.id: c
        for c in db.query(EvaluacionCriterio)
        .filter(EvaluacionCriterio.id.in_(criterio_ids))
        .all()
    } if criterio_ids else {}

    for r in body.respuestas:
        crit = criterios.get(r.criterio_id)
        if crit is None or not crit.activo:
            raise HTTPException(
                status_code=400, detail=f"Criterio {r.criterio_id} inválido o inactivo"
            )
        if r.puntaje is None:
            if not crit.permite_na:
                raise HTTPException(
                    status_code=400,
                    detail=f"El criterio '{crit.label}' no admite 'No aplica'",
                )
        elif not isinstance(r.puntaje, int) or not (1 <= r.puntaje <= 5):
            raise HTTPException(
                status_code=400,
                detail=f"El puntaje del criterio '{crit.label}' debe ser un entero de 1 a 5",
            )

    ev = Evaluacion(
        evaluado_lawyer_id=body.evaluado_lawyer_id,
        evaluador_email=body.evaluador_email,
        periodo=periodo,
        comentarios=body.comentarios,  # required + non-blank (validated)
    )
    ev.respuestas = [
        EvaluacionRespuesta(criterio_id=r.criterio_id, puntaje=r.puntaje)
        for r in body.respuestas
    ]
    db.add(ev)
    db.commit()
    db.refresh(ev)

    return EvaluacionCreated(
        id=ev.id,
        evaluado_lawyer_id=ev.evaluado_lawyer_id,
        evaluador_email=ev.evaluador_email,
        comentarios=ev.comentarios,
        respuestas=[
            RespuestaInput(criterio_id=r.criterio_id, puntaje=r.puntaje)
            for r in ev.respuestas
        ],
    )


# --------------------------------------------------------------------------- #
# Reset de una evaluación (admin)
# --------------------------------------------------------------------------- #
@router.post("/reset")
async def reset_evaluacion(
    body: EvaluacionReset,
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Delete the evaluation matching (evaluado, evaluador, periodo), freeing that
    monthly slot so the evaluador can submit again for that person that month.

    Its ``EvaluacionRespuesta`` rows are removed via the ORM delete-orphan
    cascade on ``Evaluacion.respuestas``.
    """
    email = (body.evaluador_email or "").strip().lower()
    ev = (
        db.query(Evaluacion)
        .filter(
            Evaluacion.evaluado_lawyer_id == body.evaluado_lawyer_id,
            func.lower(Evaluacion.evaluador_email) == email,
            Evaluacion.periodo == body.periodo,
        )
        .first()
    )
    if ev is None:
        raise HTTPException(status_code=404, detail="No se encontró esa evaluación.")
    db.delete(ev)  # cascades to respuestas (delete-orphan)
    db.commit()
    return {"deleted": True}


# --------------------------------------------------------------------------- #
# Resultados (admin)
# --------------------------------------------------------------------------- #
@router.get("/resultados", response_model=List[ResultadoEvaluable])
async def resultados(
    periodo: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Per-evaluable aggregates. Averages ignore NULL puntajes (N/A). Includes
    every active evaluable, plus anyone who has evaluations (even if removed).

    When ``periodo`` ("YYYY-MM") is given, only evaluations of that month are
    aggregated; omitted means all months (unchanged behavior)."""
    criterios = db.query(EvaluacionCriterio).all()
    criterio_meta = {c.id: c for c in criterios}

    # Evaluables to report: active ones + any lawyer with at least one evaluation.
    lawyer_ids: dict[int, str] = {}
    for e in (
        db.query(EvaluacionEvaluable)
        .options(joinedload(EvaluacionEvaluable.lawyer))
        .filter(EvaluacionEvaluable.activo.is_(True))
        .all()
    ):
        lawyer_ids[e.lawyer_id] = e.lawyer.name if e.lawyer else f"#{e.lawyer_id}"

    ev_query = db.query(Evaluacion).options(
        joinedload(Evaluacion.evaluado),
        joinedload(Evaluacion.respuestas),
    )
    if periodo:
        ev_query = ev_query.filter(Evaluacion.periodo == periodo)
    evaluaciones = ev_query.all()
    for ev in evaluaciones:
        if ev.evaluado_lawyer_id not in lawyer_ids:
            lawyer_ids[ev.evaluado_lawyer_id] = (
                ev.evaluado.name if ev.evaluado else f"#{ev.evaluado_lawyer_id}"
            )

    # Group evaluations by evaluado.
    by_evaluado: dict[int, list] = {lid: [] for lid in lawyer_ids}
    for ev in evaluaciones:
        by_evaluado.setdefault(ev.evaluado_lawyer_id, []).append(ev)

    out: List[ResultadoEvaluable] = []
    for lid, nombre in lawyer_ids.items():
        evs = by_evaluado.get(lid, [])
        # per-criterio accumulation: criterio_id -> [sum, count] (non-null only)
        per_crit: dict[int, list] = {}
        all_scores: list[int] = []
        comentarios: list[str] = []
        for ev in evs:
            if ev.comentarios:
                comentarios.append(ev.comentarios)
            for r in ev.respuestas:
                acc = per_crit.setdefault(r.criterio_id, [0, 0])
                if r.puntaje is not None:
                    acc[0] += r.puntaje
                    acc[1] += 1
                    all_scores.append(r.puntaje)

        por_criterio = []
        for cid, (total, n) in per_crit.items():
            meta = criterio_meta.get(cid)
            por_criterio.append(
                ResultadoCriterio(
                    criterio_id=cid,
                    label=meta.label if meta else f"#{cid}",
                    grupo=meta.grupo if meta else "",
                    promedio=(round(total / n, 2) if n else None),
                    n=n,
                )
            )
        por_criterio.sort(key=lambda x: (x.grupo, x.criterio_id))

        promedio_general = (
            round(sum(all_scores) / len(all_scores), 2) if all_scores else None
        )
        out.append(
            ResultadoEvaluable(
                lawyer_id=lid,
                nombre=nombre,
                total_evaluaciones=len(evs),
                promedio_general=promedio_general,
                por_criterio=por_criterio,
                comentarios=comentarios,
            )
        )

    # Deterministic sort: promedio_general desc (nulls last), then nombre.
    out.sort(key=lambda r: (r.promedio_general is None, -(r.promedio_general or 0), r.nombre))
    return out


# --------------------------------------------------------------------------- #
# Periodos (admin)
# --------------------------------------------------------------------------- #
@router.get("/periodos", response_model=List[str])
async def periodos(
    db: Session = Depends(get_db),
    _admin_rut: str = Depends(require_admin),
):
    """Distinct ``periodo`` values ("YYYY-MM") present in evaluaciones, most
    recent first — so the frontend month selector can populate."""
    rows = (
        db.query(Evaluacion.periodo)
        .distinct()
        .order_by(Evaluacion.periodo.desc())
        .all()
    )
    return [p for (p,) in rows]
