"""An incorporated case (patrocinio pendiente) must be visible to, and alert, its lawyer."""

import pytest

from app.api.deps import resolve_case_scope
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.lawyer_roster import case_ids_for_abogado, clear_case_scope_cache, resolve_case_alert_recipients

RUT, OTHER_RUT = "12345678-5", "99999999-9"


@pytest.fixture(autouse=True)
def _scope_cache():
    clear_case_scope_cache()
    yield
    clear_case_scope_cache()


@pytest.fixture
def world(db):
    lw = Lawyer(rut=RUT, name="Eduardo", role="lawyer", is_active=True)
    other = Lawyer(rut=OTHER_RUT, name="Otro", role="lawyer", is_active=True)
    court = Court(code="C4", name="4 Juzgado", region="RM", type="civil")
    db.add_all([lw, other, court])
    db.flush()
    return lw, other, court


def _case(db, lw, court, rol, pendiente, litigante_rut=None):
    case = Case(lawyer_id=lw.id, court_id=court.id, rol=rol, competencia="civil", patrocinio_pendiente=pendiente)
    db.add(case)
    db.flush()
    if litigante_rut:
        db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=litigante_rut, nombre="X",
                             persona_type="NATURAL", natural_key=f"{case.id}-{litigante_rut}"))
        db.flush()
    db.commit()
    return case


def test_case_with_only_lawyer_id_link_and_foreign_litigantes_is_in_scope(db, world):
    lw, other, court = world
    pending = _case(db, lw, court, "C-1-2026", True, litigante_rut=OTHER_RUT)

    assert pending.id in resolve_case_scope(db, {"sub": RUT})
    assert pending.id in case_ids_for_abogado(db, RUT, RUT)


def test_non_pending_case_with_litigantes_keeps_litigante_only_visibility(db, world):
    lw, other, court = world
    regular = _case(db, lw, court, "C-2-2026", False, litigante_rut=OTHER_RUT)

    assert regular.id not in resolve_case_scope(db, {"sub": RUT})
    assert regular.id not in case_ids_for_abogado(db, RUT, RUT)
    assert regular.id in resolve_case_scope(db, {"sub": OTHER_RUT})


def test_pending_case_is_not_visible_to_other_lawyers(db, world):
    lw, other, court = world
    pending = _case(db, lw, court, "C-1-2026", True, litigante_rut=OTHER_RUT + "x")

    assert pending.id not in resolve_case_scope(db, {"sub": OTHER_RUT})


def test_alert_recipients_fall_back_to_the_owner_for_a_pending_case(db, world):
    lw, other, court = world
    pending = _case(db, lw, court, "C-1-2026", True, litigante_rut="55555555-5")

    assert [r.id for r in resolve_case_alert_recipients(db, pending)] == [lw.id]


def test_alert_recipients_unchanged_when_the_case_has_firm_litigantes_or_is_not_pending(db, world):
    lw, other, court = world
    firm = _case(db, lw, court, "C-3-2026", True, litigante_rut=OTHER_RUT)
    regular = _case(db, lw, court, "C-4-2026", False, litigante_rut="55555555-5")

    assert [r.id for r in resolve_case_alert_recipients(db, firm)] == [other.id]
    assert resolve_case_alert_recipients(db, regular) == []
