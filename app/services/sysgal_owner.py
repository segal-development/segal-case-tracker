"""Sysgal as the primary source of a case's responsible lawyer (kill switch, resolver, report).

Precedence (decided in ``app.services.lawyer_roster``, which consumes this module):
manual override ``cases.assigned_lawyer_id`` > the active Sysgal lawyer of the case's OWN link >
PJUD (abogado-of-record litigantes). The switch ``sysgal_es_fuente_duenio`` (goals, 1 = on,
default 0) turns the Sysgal layer on; at 0 nothing here is consulted by the resolver.

PRIVACY: nothing here logs a Sysgal payload.
"""
import threading
import time
from datetime import datetime
from typing import NamedTuple, Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import app_settings
from app.services.matriz_sysgal import ACTIVA

#: How long a process trusts its last read of the switch (a flush of the switch in this
#: process invalidates it at once, see lawyer_roster._scope_cache_on_flush).
SWITCH_CACHE_SECONDS = 10
_clock = time.monotonic
_switch_cache: Optional[tuple[float, bool]] = None  # (expires_at, value)
_switch_lock = threading.Lock()


def clear_switch_cache() -> None:
    global _switch_cache
    with _switch_lock:
        _switch_cache = None


def sysgal_duenio_enabled(db: Session) -> bool:
    """True iff the ``sysgal_es_fuente_duenio`` goal is exactly 1 (read with a short cache)."""
    global _switch_cache
    with _switch_lock:
        entry = _switch_cache
        if entry is not None and _clock() < entry[0]:
            return entry[1]
    value = app_settings.get_int(db, app_settings.KEY_SYSGAL_DUENIO, app_settings.DEFAULT_SYSGAL_DUENIO) == 1
    with _switch_lock:
        _switch_cache = (_clock() + SWITCH_CACHE_SECONDS, value)
    return value


class SysgalOwner(NamedTuple):
    lawyer_id: int
    rut: str
    nombre: str


def sysgal_primary_owner_by_case(
    db: Session, *, competencia: Optional[str] = None, case_id: Optional[int] = None
) -> dict[int, SysgalOwner]:
    """``{case_id: SysgalOwner}`` decided by the case's OWN Sysgal link (ONE query, no N+1).

    Among the case's ACTIVE, non-eliminada rows the most recently updated wins (ties: highest
    ``id``); only then is its ``abogado_sysgal_id`` mapped (``sysgal_usuarios.lawyer_id``) to an
    ACTIVE lawyer. If that winner is unmapped/inactive the case has NO Sysgal owner (it falls
    through to PJUD; an older row never decides). No sibling-cuaderno inheritance. Pure read.
    """
    query = (
        db.query(SysgalCausa.case_id, Lawyer.id, Lawyer.rut, Lawyer.name)
        .join(Case, Case.id == SysgalCausa.case_id)
        .outerjoin(SysgalUsuario, SysgalUsuario.sysgal_id == SysgalCausa.abogado_sysgal_id)
        .outerjoin(Lawyer, and_(Lawyer.id == SysgalUsuario.lawyer_id, Lawyer.is_active.is_(True)))
        .filter(
            SysgalCausa.case_id.isnot(None),
            SysgalCausa.eliminada.is_(False),
            func.lower(func.trim(SysgalCausa.estado)) == ACTIVA,
        )
    )
    if competencia is not None:
        query = query.filter(Case.competencia == competencia)
    if case_id is not None:
        query = query.filter(SysgalCausa.case_id == case_id)
    rows = query.order_by(func.coalesce(SysgalCausa.sysgal_updated_at, datetime.min), SysgalCausa.id).all()
    winners = {cid: (lid, rut, name) for cid, lid, rut, name in rows}  # later rows overwrite earlier
    return {cid: SysgalOwner(*w) for cid, w in winners.items() if w[0] is not None}


def reporte_cambio_duenio(db: Session) -> dict[str, dict[str, int]]:
    """``{lawyer name: {gana, pierde}}``: cases each lawyer would gain/lose if the switch were turned on.

    Read-only; never touches the switch. Compares who sees each case with PJUD only vs with the
    Sysgal layer, using the same resolver the scope uses (so it is exactly the scope delta).
    Lawyers with no change are omitted. Duplicate names are disambiguated with the RUT.
    """
    from app.services.lawyer_roster import ALL_ABOGADO, _abogado_litigantes_by_case
    from app.utils.rut import normalize_rut

    def viewers(sysgal: bool) -> dict[int, set[str]]:
        return {
            cid: {normalize_rut(lit.rut) for lit in lits if lit.rut and lit.participante in ALL_ABOGADO}
            for cid, lits in _abogado_litigantes_by_case(db, sysgal=sysgal).items()
        }

    before, after = viewers(False), viewers(True)
    gana: dict[str, int] = {}
    pierde: dict[str, int] = {}
    for cid in before.keys() | after.keys():
        b, a = before.get(cid, set()), after.get(cid, set())
        for rut in a - b:
            gana[rut] = gana.get(rut, 0) + 1
        for rut in b - a:
            pierde[rut] = pierde.get(rut, 0) + 1

    ruts = gana.keys() | pierde.keys()
    lawyers = [lw for lw in db.query(Lawyer).all() if lw.rut and normalize_rut(lw.rut) in ruts]
    names = [lw.name for lw in lawyers]
    report: dict[str, dict[str, int]] = {}
    for lw in sorted(lawyers, key=lambda lw: lw.id):
        key = lw.name if names.count(lw.name) == 1 else f"{lw.name} ({lw.rut})"
        norm = normalize_rut(lw.rut)
        report[key] = {"gana": gana.get(norm, 0), "pierde": pierde.get(norm, 0)}
    return report
