"""External read-only ``GET /excepciones`` endpoint for the Sysgal CRM.

Audit of the excepciones plazo (art. 459 CPC, 8 días hábiles, Saturday counts)
and of the ratification plazo (3 días hábiles) for every causa of one client.

Design rules — this answers questions about FATAL plazos, so it never invents:

* "Presentó" and "a tiempo" are separate answers. ``excepciones.presentadas``
  says whether the filing exists in PJUD; ``veredicto`` says whether it was on
  time, and is ``presentado_sin_ancla`` when there is no plazo to compare to.
* The filing is ``Movement.procedure == 'Escrito'`` + ``Opone excepciones``.
  A ``Resolución`` with the same text is the court's ruling, not the filing.
* ``sin_determinar`` is an explicit, valid verdict.
* The verdict is evaluated live with ``deadline_verdict.evaluate_deadline``
  (pure) over the same movements shown in the response, so ``presentadas`` and
  ``veredicto`` can never contradict each other. The persisted
  ``CaseDeadline.verdict`` is deliberately not read: it is NULL on rows still in
  force and can lag behind new movements.
* Scope is EVERY causa of the client (``all_case_ids_for_cliente``), not only
  active ones: this is a historical audit and a terminated causa still matters.
  ``estado_causa`` is returned so the consumer can tell them apart.

Read-only: missing plazo rows are NOT created here (that is the backfill).
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.sysgal._scope import all_case_ids_for_cliente
from app.api.sysgal.deps import require_sysgal_key
from app.core.deadlines_config import DEADLINE_DISCLAIMER, DeadlineType
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.movement import Movement
from app.services.business_days import count_business_days_remaining
from app.services.deadline_engine import _today_chile
from app.services.deadline_verdict import Verdict, evaluate_deadline
from app.utils.rut import normalize_rut

router = APIRouter()

SIN_DETERMINAR = "sin_determinar"

_MOTIVOS: Dict[str, str] = {
    Verdict.CUMPLIDO.value: "Las excepciones se presentaron dentro del plazo.",
    Verdict.FUERA_DE_PLAZO.value: "Las excepciones se presentaron después del vencimiento del plazo.",
    Verdict.REGISTRO_TARDIO.value: (
        "El escrito de excepciones se registró después del vencimiento, pero dentro del margen "
        "de atraso con que PJUD publica. No se puede afirmar que fue tardío: verifica la fecha "
        "de presentación en el expediente."
    ),
    Verdict.NO_CUMPLIDO.value: "El plazo venció y no hay escrito de excepciones en PJUD.",
    Verdict.PRESENTADO_SIN_ANCLA.value: (
        "Las excepciones se presentaron, pero no hay plazo calculado para esta causa, "
        "así que no se puede decir si fue a tiempo."
    ),
    SIN_DETERMINAR: "Hoy no hay datos suficientes para afirmar si se cumplió o no.",
}
_MOTIVO_SIN_MOVIMIENTOS = "No hay movimientos de PJUD cargados para esta causa."
_MOTIVO_NO_EJECUTADO = (
    "El RUT consultado no figura como demandado en esta causa, "
    "por lo que las excepciones no le corresponden."
)


class PlazoOut(BaseModel):
    desde: date
    vence: date
    # Signed: negative means the plazo already expired.
    dias_habiles_restantes: int
    fundamento_legal: Optional[str] = None


class ExcepcionesOut(BaseModel):
    # None = cannot tell (no movements loaded, or not the ejecutado's filing).
    presentadas: Optional[bool] = None
    fecha: Optional[date] = None
    movimiento_id: Optional[int] = None


class VeredictoOut(BaseModel):
    valor: str
    motivo: str


class RatificacionOut(BaseModel):
    desde: date
    vence: date
    dias_habiles_restantes: int
    vencida: bool
    # True only when recorded as fulfilled, False only when an auditor marked it
    # not fulfilled; None = nothing proves either.
    cumplida: Optional[bool] = None


class SysgalExcepcionesItem(BaseModel):
    rol: str
    tribunal: Optional[str] = None
    caratulado: str
    estado_causa: Optional[str] = None
    rol_cliente: List[str]
    plazo_excepciones: Optional[PlazoOut] = None
    excepciones: ExcepcionesOut
    veredicto: VeredictoOut
    ratificacion: Optional[RatificacionOut] = None


class SysgalExcepcionesResponse(BaseModel):
    disclaimer: str
    cliente_rut: str
    causas: List[SysgalExcepcionesItem]


def _pick_deadline(rows: List[CaseDeadline]) -> Optional[CaseDeadline]:
    """Current row of one plazo type: non-superseded first, then newest anchor.

    A ``superseded`` row is stale (re-anchored or the case moved on), so it is
    only used when nothing else exists.
    """
    if not rows:
        return None
    return sorted(rows, key=lambda r: (r.status == "superseded", -r.triggered_at.toordinal()))[0]


def _is_ejecutado(roles: List[str]) -> bool:
    return any("DDO" in (r or "").upper() for r in roles)


def _build_item(
    case: Case,
    court_name: Optional[str],
    roles: List[str],
    movements: List[Movement],
    excepciones_row: Optional[CaseDeadline],
    poder_row: Optional[CaseDeadline],
    today: date,
) -> SysgalExcepcionesItem:
    plazo = None
    if excepciones_row is not None:
        plazo = PlazoOut(
            desde=excepciones_row.triggered_at,
            vence=excepciones_row.due_date,
            dias_habiles_restantes=count_business_days_remaining(excepciones_row.due_date, today),
            fundamento_legal=excepciones_row.legal_basis,
        )

    if not movements:
        exc = ExcepcionesOut()
        verdict_value, motivo = SIN_DETERMINAR, _MOTIVO_SIN_MOVIMIENTOS
    elif not _is_ejecutado(roles):
        exc = ExcepcionesOut()
        verdict_value, motivo = SIN_DETERMINAR, _MOTIVO_NO_EJECUTADO
    else:
        result = evaluate_deadline(
            DeadlineType.EXCEPCIONES_8D.value,
            excepciones_row.due_date if excepciones_row is not None else None,
            movements,
            today,
        )
        # A verdict other than None/no_cumplido exists only when an act was found.
        filed = result.verdict in (
            Verdict.CUMPLIDO,
            Verdict.FUERA_DE_PLAZO,
            Verdict.REGISTRO_TARDIO,
            Verdict.PRESENTADO_SIN_ANCLA,
        )
        exc = ExcepcionesOut(
            presentadas=filed,
            fecha=result.acted_on if filed else None,
            movimiento_id=result.movement_id if filed else None,
        )
        verdict_value = result.verdict.value if result.verdict else SIN_DETERMINAR
        motivo = _MOTIVOS[verdict_value]

    rat = None
    if poder_row is not None:
        rat = RatificacionOut(
            desde=poder_row.triggered_at,
            vence=poder_row.due_date,
            dias_habiles_restantes=count_business_days_remaining(poder_row.due_date, today),
            vencida=poder_row.due_date < today,
            cumplida={"cumplido": True, "no_cumplido": False}.get(poder_row.status),
        )

    return SysgalExcepcionesItem(
        rol=case.rol,
        tribunal=court_name,
        caratulado=f"{case.plaintiff or ''}/{case.defendant or ''}",
        estado_causa=case.status,
        rol_cliente=roles,
        plazo_excepciones=plazo,
        excepciones=exc,
        veredicto=VeredictoOut(valor=verdict_value, motivo=motivo),
        ratificacion=rat,
    )


@router.get("/excepciones", response_model=SysgalExcepcionesResponse)
def get_excepciones(
    cliente_rut: str = Query(..., description="RUT del cliente (litigante) a consultar"),
    db: Session = Depends(get_db),
    _key=Depends(require_sysgal_key),
) -> SysgalExcepcionesResponse:
    """Per-causa audit of excepciones and ratification for one client (read-only)."""
    rut = normalize_rut(cliente_rut)
    case_ids = all_case_ids_for_cliente(db, cliente_rut)
    if not case_ids:
        return SysgalExcepcionesResponse(
            disclaimer=DEADLINE_DISCLAIMER, cliente_rut=rut, causas=[]
        )

    today = _today_chile()
    cases = db.query(Case).filter(Case.id.in_(case_ids)).order_by(Case.rol, Case.id).all()

    court_ids = {c.court_id for c in cases if c.court_id is not None}
    court_names = (
        {cid: n for cid, n in db.query(Court.id, Court.name).filter(Court.id.in_(court_ids))}
        if court_ids
        else {}
    )

    roles_by_case: Dict[int, List[str]] = {}
    for cid, participante in (
        db.query(CaseLitigante.case_id, CaseLitigante.participante)
        .filter(CaseLitigante.case_id.in_(case_ids), CaseLitigante.rut == rut)
        .order_by(CaseLitigante.id)
    ):
        roles_by_case.setdefault(cid, []).append(participante)

    movements_by_case: Dict[int, List[Movement]] = {}
    for mv in (
        db.query(Movement)
        .filter(Movement.case_id.in_(case_ids))
        .order_by(Movement.movement_date, Movement.id)
    ):
        movements_by_case.setdefault(mv.case_id, []).append(mv)

    deadlines: Dict[tuple, List[CaseDeadline]] = {}
    for row in db.query(CaseDeadline).filter(
        CaseDeadline.case_id.in_(case_ids),
        CaseDeadline.deadline_type.in_(
            [DeadlineType.EXCEPCIONES_8D.value, DeadlineType.ACREDITAR_PODER_3D.value]
        ),
    ):
        deadlines.setdefault((row.case_id, row.deadline_type), []).append(row)

    items = []
    for case in cases:
        poder = _pick_deadline(deadlines.get((case.id, DeadlineType.ACREDITAR_PODER_3D.value), []))
        if poder is not None and poder.status == "superseded":
            poder = None  # a stale ratification plazo says nothing about today
        items.append(
            _build_item(
                case,
                court_names.get(case.court_id),
                roles_by_case.get(case.id, []),
                movements_by_case.get(case.id, []),
                _pick_deadline(
                    [
                        r
                        for r in deadlines.get((case.id, DeadlineType.EXCEPCIONES_8D.value), [])
                        # Superseded without a verdict = re-anchored: stale due_date.
                        if r.status != "superseded" or r.verdict is not None
                    ]
                ),
                poder,
                today,
            )
        )
    return SysgalExcepcionesResponse(disclaimer=DEADLINE_DISCLAIMER, cliente_rut=rut, causas=items)
