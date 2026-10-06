"""Shared scoping helper for the external Sysgal CRM API.

Every Sysgal endpoint is scoped to a single client RUT and to that client's
ACTIVE causas only. This helper centralizes that resolution so each endpoint
(``/causas``, ``/plazos``, ``/novedades``, ``/buscar``) applies the exact same
rule: a case is in scope when the normalized client RUT appears as a litigante
(any party role) on an ``active`` case.
"""

from typing import List

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.utils.rut import normalize_rut


def active_case_ids_for_cliente(db: Session, cliente_rut: str) -> List[int]:
    """Case ids of ACTIVE causas where ``cliente_rut`` is a litigante.

    Matches the client by normalized RUT against any ``CaseLitigante`` row
    (any party role), then keeps only cases with ``status == "active"``
    (archived/closed excluded). Returns a de-duplicated list of ``Case.id``.
    """
    rut = normalize_rut(cliente_rut)
    rows = (
        db.query(Case.id)
        .join(CaseLitigante, CaseLitigante.case_id == Case.id)
        .filter(CaseLitigante.rut == rut, Case.status == "active")
        .distinct()
        .all()
    )
    return [cid for (cid,) in rows]


def all_case_ids_for_cliente(db: Session, cliente_rut: str) -> List[int]:
    """Case ids of EVERY causa (any ``status``) where ``cliente_rut`` is a litigante.

    Used by audit-style endpoints. "Did this client file the excepciones on
    time?" is a historical question: a terminated or archived causa still
    answers it, and hiding it would turn a missed fatal plazo into silence.
    Same RUT matching as ``active_case_ids_for_cliente``, minus the status
    filter. Callers must surface ``Case.status`` so the consumer can tell.
    """
    rut = normalize_rut(cliente_rut)
    rows = (
        db.query(Case.id)
        .join(CaseLitigante, CaseLitigante.case_id == Case.id)
        .filter(CaseLitigante.rut == rut)
        .distinct()
        .all()
    )
    return [cid for (cid,) in rows]
