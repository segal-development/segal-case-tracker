"""Poda de causas sin cobertura comercial.

El estudio scrapea causas del PJUD para cada abogado de la firma. Una parte
de esa cartera pertenece a clientes que ya NO tienen contrato vigente (la
cobertura comercial en Sysgal quedó ``caducado`` para todas sus partes).
Seguir scrapeando esas causas quema presupuesto de scraping — horas de
browser, riesgo de bloqueo/challenge de PJUD — sobre trabajo que ya no le
genera valor al estudio.

La poda NO borra la causa ni su historial: la marca (``Case.poda_at``) para
que ``_select_cases_for_detail_rotation`` (``app.services.sync_service``) deje
de visitarla. Es reversible en la práctica (basta con limpiar la marca) y
completamente auditable (``poda_motivo`` + ``poda_por_rut``).

Regla de podabilidad (exacta, ver ``_coberturas_evaluables``): una causa es
podable si y solo si tiene al menos una parte evaluable (no representante) Y
el conjunto de coberturas de esas partes es exactamente ``{"caducado"}``. Un
desconocido (parte sin dato en el caché de Sysgal, o ausente del caché) NUNCA
es podable — la ausencia de información no autoriza a dejar de mirar la
causa. Un moroso tampoco es podable: sigue siendo cliente.

Guarda de seguridad: por defecto ``aplicar_poda`` excluye las causas con
movimiento reciente (``last_movement_at`` dentro de ``dias_movimiento_reciente``
días) — una causa que se está moviendo en tribunales no se debe dejar de
mirar aunque haya perdido cobertura comercial. Solo se incluyen si quien llama
pasa explícitamente ``incluir_con_movimiento=True``.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.services.sysgal_cobertura import derive_cobertura
from app.services.sysgal_sync import _REPRESENTATIVE_PREFIXES
from app.utils.rut import clean_rut

logger = logging.getLogger(__name__)

#: Motivo grabado en ``Case.poda_motivo`` para toda poda hecha por este motor.
MOTIVO_SIN_COBERTURA = "sysgal_caducado"

#: Ventana por defecto de "movimiento reciente" — una causa que se movió hace
#: menos de esto en tribunales no se poda aunque haya perdido cobertura.
DIAS_MOVIMIENTO_RECIENTE = 90

#: Causas por transacción. Mismo motivo que ``asignacion_engine``: el scraping
#: corre contra la misma base a través del proxy, así que conviene commitear
#: seguido en vez de sostener una transacción larga que bloquee filas.
_COMMIT_CHUNK = 200


@dataclass
class ClasificacionPoda:
    """Reparto de la cartera según la cobertura comercial de sus partes.

    Las cinco categorías son mutuamente excluyentes y suman ``total_causas``.
    Las causas YA podadas (``Case.poda_at`` no nulo) quedan completamente
    afuera de este reparto — ya no son parte de la cartera a evaluar, así que
    ni siquiera se cuentan en ``total_causas`` (ver ``clasificar_cartera``).
    """

    podables: List[int] = field(default_factory=list)  # case_ids con todas las partes caducadas
    con_cliente_activo: int = 0
    con_cliente_moroso: int = 0
    # Cobertura desconocida en juego: incluye tanto la causa donde NINGUNA
    # parte está en el caché de Sysgal como la causa donde conviven partes
    # caducadas con partes sin dato — en ambos casos falta información para
    # podar (regla 3), así que ninguna es podable.
    sin_parte_conocida: int = 0
    sin_partes_evaluables: int = 0  # sin litigantes con RUT (excluyendo representantes)
    total_causas: int = 0


def _ruts_evaluables_por_causa(db: Session) -> Dict[int, Set[str]]:
    """RUTs evaluables (parte, no representante) de cada causa, deduplicados.

    Reutiliza ``_REPRESENTATIVE_PREFIXES`` de ``sysgal_sync`` para no duplicar
    la regla de qué cuenta como representante (AB./ABG./AP.).
    """
    rows = (
        db.query(CaseLitigante.case_id, CaseLitigante.rut)
        .filter(
            CaseLitigante.rut != "",
            *[
                ~CaseLitigante.participante.ilike(f"{prefix}%")
                for prefix in _REPRESENTATIVE_PREFIXES
            ],
        )
        .all()
    )
    por_causa: Dict[int, Set[str]] = {}
    for case_id, rut in rows:
        cleaned = clean_rut(rut) if rut else ""
        if not cleaned:
            continue
        por_causa.setdefault(case_id, set()).add(cleaned)
    return por_causa


def _cobertura_cache(db: Session, ruts: Set[str]) -> Dict[str, ClienteSysgalEstado]:
    """Carga en un solo query las filas del caché de Sysgal para *ruts*."""
    if not ruts:
        return {}
    filas = (
        db.query(ClienteSysgalEstado)
        .filter(ClienteSysgalEstado.rut.in_(ruts))
        .all()
    )
    return {fila.rut: fila for fila in filas}


def _cobertura_de(estado: Optional[ClienteSysgalEstado]) -> str:
    """Cobertura de un RUT dado su estado cacheado (``None`` = ausente del caché)."""
    if estado is None:
        return derive_cobertura(None, None, encontrado=False)
    return derive_cobertura(estado.estado_codigo, estado.vigencia_hasta, estado.encontrado)


def coberturas_por_causa(db: Session, case_ids: List[int]) -> Dict[int, Dict[str, str]]:
    """RUT -> cobertura de cada parte evaluable, para las causas de *case_ids*.

    Expuesto para reporting (p. ej. ``scripts/poda_dry_run.py``): toda la
    lógica de qué es una parte evaluable y cómo se deriva su cobertura vive
    acá, así el script que la imprime no reimplementa nada.
    """
    if not case_ids:
        return {}
    ids = set(case_ids)
    ruts_por_causa = {
        cid: ruts for cid, ruts in _ruts_evaluables_por_causa(db).items() if cid in ids
    }
    todos_los_ruts = {r for ruts in ruts_por_causa.values() for r in ruts}
    cache = _cobertura_cache(db, todos_los_ruts)
    return {
        cid: {rut: _cobertura_de(cache.get(rut)) for rut in ruts}
        for cid, ruts in ruts_por_causa.items()
    }


def clasificar_cartera(db: Session) -> ClasificacionPoda:
    """Reparte la cartera (no podada aún) según la cobertura de sus partes.

    De SOLO LECTURA: no escribe nada. Cada causa cae en exactamente una
    categoría de ``ClasificacionPoda``.
    """
    resultado = ClasificacionPoda()

    causas = db.query(Case.id).filter(Case.poda_at.is_(None)).all()
    case_ids = [cid for (cid,) in causas]
    resultado.total_causas = len(case_ids)
    if not case_ids:
        return resultado

    ruts_por_causa = _ruts_evaluables_por_causa(db)
    todos_los_ruts = {r for ruts in ruts_por_causa.values() for r in ruts}
    cache = _cobertura_cache(db, todos_los_ruts)

    for case_id in case_ids:
        ruts = ruts_por_causa.get(case_id)
        if not ruts:
            resultado.sin_partes_evaluables += 1
            continue

        coberturas = {_cobertura_de(cache.get(rut)) for rut in ruts}

        if "activo" in coberturas:
            resultado.con_cliente_activo += 1
        elif "moroso" in coberturas:
            resultado.con_cliente_moroso += 1
        elif coberturas != {"caducado"}:
            # Queda "sin_dato" en el conjunto (puro o mezclado con caducado).
            resultado.sin_parte_conocida += 1
        else:
            resultado.podables.append(case_id)

    return resultado


def aplicar_poda(
    db: Session,
    *,
    actor_rut: str,
    dias_movimiento_reciente: int = DIAS_MOVIMIENTO_RECIENTE,
    incluir_con_movimiento: bool = False,
    limite: Optional[int] = None,
    dry_run: bool = True,
) -> dict:
    """Arma —y opcionalmente aplica— la poda de la cartera sin cobertura.

    ``dry_run`` por defecto: se puede ver qué haría antes de escribir nada,
    lo que corresponde para una operación que puede tocar buena parte de la
    cartera de una sola corrida.

    Idempotente: ``clasificar_cartera`` ya excluye del reparto toda causa con
    ``poda_at`` no nulo, así que una causa ya podada nunca vuelve a aparecer
    como podable en una corrida posterior.
    """
    clasificacion = clasificar_cartera(db)
    podable_ids = clasificacion.podables

    resultado = {
        "podables": len(podable_ids),
        "podadas": 0,
        "salteadas_por_movimiento": 0,
        "dry_run": dry_run,
    }

    if not podable_ids:
        return resultado

    cutoff = datetime.utcnow() - timedelta(days=dias_movimiento_reciente)

    candidatas = (
        db.query(Case)
        .filter(Case.id.in_(podable_ids))
        .order_by(Case.id)
        .all()
    )

    a_podar: List[Case] = []
    salteadas = 0
    for case in candidatas:
        tiene_movimiento_reciente = (
            case.last_movement_at is not None and case.last_movement_at >= cutoff
        )
        if tiene_movimiento_reciente and not incluir_con_movimiento:
            salteadas += 1
            continue
        a_podar.append(case)

    if limite is not None:
        a_podar = a_podar[:limite]

    resultado["podadas"] = len(a_podar)
    resultado["salteadas_por_movimiento"] = salteadas

    if dry_run:
        return resultado

    ahora = datetime.utcnow()
    for start in range(0, len(a_podar), _COMMIT_CHUNK):
        for case in a_podar[start : start + _COMMIT_CHUNK]:
            case.poda_at = ahora
            case.poda_motivo = MOTIVO_SIN_COBERTURA
            case.poda_por_rut = actor_rut
        db.commit()

    logger.info(
        "poda de causas: %s podadas de %s podables, salteadas_por_movimiento=%s",
        resultado["podadas"],
        resultado["podables"],
        resultado["salteadas_por_movimiento"],
    )
    return resultado
