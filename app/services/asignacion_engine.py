"""Motor de asignación automática de cartera por nivel.

Reparte las causas que todavía no tienen abogado asignado entre los abogados
de la firma cuyo ``nivel`` satisface lo que exige la matriz de cada causa,
balanceando la carga resuelta de cada uno.

Tres reglas que este motor NO negocia, porque las tres protegen algo que ya
costó caro:

* **Nunca toca ``Case.lawyer_id``.** Esa columna es procedencia de scraping y
  la usa ``existing_by_rol`` para deduplicar el sync; escribirla haría que el
  scraper recreara causas. Solo se escribe el override ``assigned_*``.
* **No reparte clasificaciones provisorias.** Una causa sin un solo movimiento
  scrapeado queda en ``matriz_origen = "sin_detalle"`` con una matriz por
  defecto, no clasificada: sobre la cartera real eso es más de la mitad de las
  causas. Repartir sobre un valor inventado le arma al abogado una cartera que
  no refleja su trabajo. Se puede pedir explícitamente con
  ``incluir_provisorias``.
* **No pisa una asignación existente.** Una causa ya asignada es una decisión
  humana tomada con más contexto del que tiene este motor.

Toda asignación queda marcada con ``MOTIVO_AUTOMATICO``, así que el lote
completo se puede identificar y deshacer.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import resolved_owner_by_case

logger = logging.getLogger(__name__)

#: Nivel(es) que puede operar cada matriz. Espejo de la regla de negocio que ya
#: aplica ``app.api.v1.asignacion``; vive acá para que el motor no dependa de
#: la capa HTTP.
NIVELES_REQUERIDOS: Dict[str, List[str]] = {
    "M1 Baja": ["junior"],
    "M1 Alta": ["pleno", "senior"],
    "M2": ["pleno", "senior"],
    "M3": ["senior"],
}

#: ``matriz_origen`` de una causa cuya matriz es un valor por defecto y no una
#: clasificación: no tiene movimientos que mirar.
ORIGEN_PROVISORIO = "sin_detalle"

#: Marca de las asignaciones hechas por este motor. Permite identificarlas y
#: revertir el lote completo sin tocar las hechas a mano.
MOTIVO_AUTOMATICO = "asignación automática por nivel"

#: Causas por transacción. El scraping corre contra la misma base a través del
#: proxy, así que el motor commitea seguido en vez de mantener una transacción
#: larga que le bloquee las filas.
_COMMIT_CHUNK = 200


@dataclass
class AsignacionPropuesta:
    """Una asignación concreta del plan."""

    case_id: int
    rol: str
    matriz: Optional[str]
    lawyer_id: int
    lawyer_name: str
    nivel: Optional[str]


@dataclass
class PlanAsignacion:
    """Resultado del motor: qué se asignaría (o se asignó) y qué quedó afuera."""

    detalle: List[AsignacionPropuesta] = field(default_factory=list)
    omitidas: Counter = field(default_factory=Counter)
    carga_final: Dict[int, int] = field(default_factory=dict)
    aplicado: bool = False

    @property
    def asignadas(self) -> int:
        return len(self.detalle)


def _abogados_por_nivel(db: Session) -> Dict[str, List[Lawyer]]:
    """Abogados activos de la firma, agrupados por nivel y en orden estable."""
    niveles = {n for req in NIVELES_REQUERIDOS.values() for n in req}
    lawyers = (
        db.query(Lawyer)
        .filter(
            Lawyer.is_firm_lawyer.is_(True),
            Lawyer.is_active.is_(True),
            Lawyer.nivel.in_(sorted(niveles)),
        )
        .order_by(Lawyer.id)
        .all()
    )
    por_nivel: Dict[str, List[Lawyer]] = {n: [] for n in niveles}
    for lw in lawyers:
        por_nivel[lw.nivel].append(lw)
    return por_nivel


def asignar_automatico(
    db: Session,
    *,
    actor_rut: str,
    incluir_provisorias: bool = False,
    limite: Optional[int] = None,
    dry_run: bool = True,
) -> PlanAsignacion:
    """Arma —y opcionalmente aplica— el reparto de la cartera sin asignar.

    ``dry_run`` por defecto: el plan se puede mirar entero antes de escribir
    nada, que es lo que corresponde para una operación que toca miles de
    causas de una vez.

    El reparto es determinista: dentro de cada nivel gana el abogado con menos
    carga resuelta y, ante empate, el de menor ``id``. Dos corridas sobre los
    mismos datos producen el mismo plan.
    """
    plan = PlanAsignacion()

    por_nivel = _abogados_por_nivel(db)
    # Carga actual con la MISMA resolución que usa el resto del sistema
    # (override > litigante > nada), para no inventar una segunda definición
    # de "cuántas causas tiene este abogado".
    carga = Counter(lw.id for lw in resolved_owner_by_case(db).values())

    candidatas = (
        db.query(Case)
        .filter(Case.assigned_lawyer_id.is_(None), Case.status != "archived")
        .order_by(Case.id)
        .all()
    )

    # Las ya asignadas ni siquiera llegan acá por el filtro, pero se cuentan
    # aparte para que el informe explique el total de la cartera.
    ya_asignadas = (
        db.query(Case)
        .filter(Case.assigned_lawyer_id.isnot(None), Case.status != "archived")
        .count()
    )
    if ya_asignadas:
        plan.omitidas["ya tiene abogado asignado"] = ya_asignadas

    elegidas: List[tuple] = []
    for case in candidatas:
        if limite is not None and len(elegidas) >= limite:
            break

        niveles = NIVELES_REQUERIDOS.get(case.matriz or "")
        if not niveles:
            plan.omitidas["matriz sin regla de nivel"] += 1
            continue

        if not incluir_provisorias and case.matriz_origen == ORIGEN_PROVISORIO:
            plan.omitidas["clasificación provisoria"] += 1
            continue

        candidatos = [lw for nivel in niveles for lw in por_nivel.get(nivel, [])]
        if not candidatos:
            plan.omitidas["sin abogado del nivel requerido"] += 1
            continue

        elegido = min(candidatos, key=lambda lw: (carga[lw.id], lw.id))
        carga[elegido.id] += 1
        elegidas.append((case, elegido))
        plan.detalle.append(
            AsignacionPropuesta(
                case_id=case.id,
                rol=case.rol,
                matriz=case.matriz,
                lawyer_id=elegido.id,
                lawyer_name=elegido.name,
                nivel=elegido.nivel,
            )
        )

    plan.carga_final = dict(carga)

    if dry_run:
        return plan

    ahora = datetime.utcnow()
    for start in range(0, len(elegidas), _COMMIT_CHUNK):
        for case, elegido in elegidas[start : start + _COMMIT_CHUNK]:
            case.assigned_lawyer_id = elegido.id
            case.assigned_at = ahora
            case.assigned_by_rut = actor_rut
            case.assigned_motivo = MOTIVO_AUTOMATICO
        db.commit()

    plan.aplicado = True
    logger.info(
        "asignación automática: %s causas repartidas, omitidas=%s",
        plan.asignadas,
        dict(plan.omitidas),
    )
    return plan
