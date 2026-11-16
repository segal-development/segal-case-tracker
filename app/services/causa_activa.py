"""One definition of "causa activa", shared by every total the firm reads.

activa = NOT ``terminada_en_sysgal`` AND contract state not ``caducado`` (``activo``, ``moroso``, ``sin_dato``
and never-computed all count). The rule lives in ``app.models.case.compute_es_activa`` (the stored
``cases.es_activa`` follows it through mapper listeners); the PJUD "archivada" state is not applied yet and
will be added there, and only there.

Excluded causas are not hidden: each total answers with ``activas_definicion`` and ``excluidas`` so a screen
can show what is left out. A causa terminada AND caducado is counted once, as terminada.
"""

from typing import Iterable, Optional

from pydantic import BaseModel

from app.models.case import Case, compute_es_activa  # noqa: F401  (re-exported: the one rule)

ACTIVAS_DEFINICION = "no terminada en Sysgal; contrato vigente, moroso o sin dato"

#: THE SQL predicate of every "how many causas" count.
CASE_ACTIVA_FILTER = Case.es_activa.is_(True)

MOTIVO_TERMINADA = "terminada_sysgal"
MOTIVO_CADUCADO = "contrato_caducado"


class Excluidas(BaseModel):
    terminada_sysgal: int = 0
    contrato_caducado: int = 0


def motivo_exclusion(terminada_en_sysgal, contrato_estado) -> Optional[str]:
    """Why a causa is out of the totals (None when it is activa)."""
    if terminada_en_sysgal:
        return MOTIVO_TERMINADA
    if contrato_estado == "caducado":
        return MOTIVO_CADUCADO
    return None


def excluidas_de(rows: Iterable[tuple]) -> dict:
    """``{terminada_sysgal, contrato_caducado}`` over ``(terminada_en_sysgal, contrato_estado)`` pairs."""
    out = {MOTIVO_TERMINADA: 0, MOTIVO_CADUCADO: 0}
    for terminada, contrato in rows:
        motivo = motivo_exclusion(terminada, contrato)
        if motivo:
            out[motivo] += 1
    return out


def excluidas_query(query) -> dict:
    """Same split over a ``Case`` query (one grouped SQL pass, whatever its size)."""
    from sqlalchemy import case as sql_case
    from sqlalchemy import func

    terminadas = func.coalesce(func.sum(sql_case((Case.terminada_en_sysgal.is_(True), 1), else_=0)), 0)
    caducadas = func.coalesce(
        func.sum(
            sql_case(
                (Case.terminada_en_sysgal.is_(False) & (Case.contrato_estado == "caducado"), 1),
                else_=0,
            )
        ),
        0,
    )
    t, c = query.order_by(None).with_entities(terminadas, caducadas).one()
    return {MOTIVO_TERMINADA: int(t), MOTIVO_CADUCADO: int(c)}


def activa_fields(excluidas: dict) -> dict:
    """The two response fields every total carries."""
    return {"activas_definicion": ACTIVAS_DEFINICION, "excluidas": Excluidas(**excluidas)}
