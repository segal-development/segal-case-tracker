"""Patrocinio/poder state of a Sysgal causa, derived from existing case data.

Read-only. States (``ESTADOS``):

* ``sin_causa_pjud``    the Sysgal causa has no linked ``Case`` (``case is None``).
* ``poder_pendiente``   the court ordered to acreditar patrocinio y poder (the
                        ``acreditar_poder_3d`` obligation, see ``poder_deadline``) and no
                        compliance movement exists after it.
* ``poder_acreditado``  that obligation was fulfilled, OR no obligation exists, the case is no
                        longer ``patrocinio_pendiente`` and a firm AB./AP. litigante is on it.
* ``escrito_presentado`` the causa is in PJUD but none of the above holds: the firm's escrito
                        was filed (case incorporated / tracked), the poder is neither requested
                        by the court nor accredited yet.

The poder obligation uses the same detector as the deadline engine
(``find_poder_obligation`` over the raw movements) and the same ownership rule as the
``acreditar_poder_3d`` alerts (``obligation_is_ours`` with ``_firm_side``): an obligation that is
the counterparty's does not make the poder pending.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.lawyer_roster import ALL_ABOGADO
from app.services.poder_deadline import (
    _day,
    find_poder_obligation,
    is_compliance_movement,
    is_extended_compliance_movement,
    is_extended_trigger,
    obligation_is_ours,
)
from app.utils.rut import normalize_rut

ESTADOS = ("escrito_presentado", "poder_pendiente", "poder_acreditado", "sin_causa_pjud")


@dataclass(frozen=True)
class PatrocinioEstado:
    estado: str
    poder_acreditado_at: datetime | None = None
    escrito_presentado_at: datetime | None = None


def _has_firm_litigante(db: Session, case: Case) -> bool:
    """An AB./AP. litigante whose RUT is an active internal ``Lawyer``."""
    ruts = {
        normalize_rut(r)
        for (r,) in db.query(CaseLitigante.rut)
        .filter(CaseLitigante.case_id == case.id, CaseLitigante.participante.in_(list(ALL_ABOGADO)))
        .all()
        if r
    }
    if not ruts:
        return False
    firm = {
        normalize_rut(r) for (r,) in db.query(Lawyer.rut).filter(Lawyer.is_active.is_(True)).all() if r
    }
    return bool(ruts & firm)


def _is_escrito(movement: Movement) -> bool:
    return (movement.procedure or "").strip().lower() == "escrito"


def derive_patrocinio_estado(db: Session, case: Case | None) -> PatrocinioEstado:
    """State of the patrocinio/poder for ``case`` (None = Sysgal causa not in PJUD)."""
    if case is None:
        return PatrocinioEstado("sin_causa_pjud")

    from app.services.deadline_engine import _firm_side

    movements = (
        db.query(Movement)
        .filter(Movement.case_id == case.id)
        .order_by(Movement.movement_date.asc(), Movement.id.asc())
        .all()
    )
    obligation = find_poder_obligation(movements)
    if obligation is not None and not obligation_is_ours(
        movements, getattr(obligation.trigger, "id", None), _firm_side(db, case)
    ):
        obligation = None

    if obligation is not None:
        trigger = obligation.trigger
        trigger_day = _day(trigger)
        # The escrito the court answered: latest party escrito on/before the resolution (heuristic).
        escritos = [m for m in movements if _is_escrito(m) and _day(m) <= trigger_day]
        escrito_at = escritos[-1].movement_date if escritos else None
        if not obligation.fulfilled:
            return PatrocinioEstado("poder_pendiente", None, escrito_at)
        extended = is_extended_trigger(trigger)
        compliance = next(
            m
            for m in movements
            if _day(m) > trigger_day
            and (is_compliance_movement(m) or (extended and is_extended_compliance_movement(m)))
        )
        return PatrocinioEstado("poder_acreditado", compliance.movement_date, escrito_at)

    if not case.patrocinio_pendiente and _has_firm_litigante(db, case):
        return PatrocinioEstado("poder_acreditado")
    return PatrocinioEstado("escrito_presentado")
