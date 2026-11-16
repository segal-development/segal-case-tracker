"""Case scope with Sysgal as primary owner: cache invalidation (odd/tasks/sysgal-fuente-principal.md S2, S5)."""
from datetime import datetime

import pytest

from app.api.deps import resolve_case_scope
from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from app.services import app_settings

PJUD_RUT, EDU_RUT, LUIS_RUT = "11111111-1", "22222222-2", "33333333-3"


@pytest.fixture
def world(db):
    court = Court(code="T-CS", name="Juzgado CS", region="RM", type="civil")
    pjud = Lawyer(rut=PJUD_RUT, name="Pjud", role="lawyer", is_active=True)
    edu = Lawyer(rut=EDU_RUT, name="Edu", role="lawyer", is_active=True)
    luis = Lawyer(rut=LUIS_RUT, name="Luis", role="lawyer", is_active=True)
    db.add_all([court, pjud, edu, luis])
    db.flush()
    u_edu = SysgalUsuario(sysgal_id=10, nombre="edu", lawyer_id=edu.id)
    u_luis = SysgalUsuario(sysgal_id=20, nombre="luis", lawyer_id=luis.id)
    case = Case(lawyer_id=pjud.id, court_id=court.id, rol="C-1-2026", competencia="civil", status="active")
    db.add_all([u_edu, u_luis, case])
    db.flush()
    db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=PJUD_RUT, nombre="Pjud",
                         persona_type="NATURAL", natural_key="k1"))
    causa = SysgalCausa(sysgal_id=1, rol="C-1-2026", rol_norm="C-1-2026", tribunal="J", estado="activa",
                        eliminada=False, abogado_sysgal_id=10, sysgal_updated_at=datetime(2026, 11, 1),
                        case_id=case.id)
    db.add(causa)
    db.commit()
    return {"case": case, "causa": causa, "u_edu": u_edu, "u_luis": u_luis, "edu": edu}


def scope(db, rut):
    return resolve_case_scope(db, {"sub": rut})


def turn_on(db):
    app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 1)
    db.commit()


def test_switch_off_scope_is_pjud_only(db, world):
    cid = world["case"].id
    assert cid in scope(db, PJUD_RUT)
    assert cid not in scope(db, EDU_RUT)


def test_switch_on_moves_the_case_to_the_sysgal_lawyer(db, world):
    cid = world["case"].id
    turn_on(db)
    assert cid in scope(db, EDU_RUT)
    assert cid not in scope(db, PJUD_RUT)


def test_flipping_the_switch_invalidates_cached_scopes(db, world):
    cid = world["case"].id
    assert cid in scope(db, PJUD_RUT) and cid not in scope(db, EDU_RUT)  # warm the cache
    turn_on(db)
    assert cid not in scope(db, PJUD_RUT) and cid in scope(db, EDU_RUT)
    app_settings.set_int(db, app_settings.KEY_SYSGAL_DUENIO, 0)
    db.commit()
    assert cid in scope(db, PJUD_RUT) and cid not in scope(db, EDU_RUT)


@pytest.mark.parametrize("change", ["abogado", "estado", "eliminada", "case_id", "usuario_lawyer", "lawyer_inactive"])
def test_sysgal_changes_invalidate_cached_scopes(db, world, change):
    cid = world["case"].id
    turn_on(db)
    assert cid in scope(db, EDU_RUT) and cid not in scope(db, LUIS_RUT) and cid not in scope(db, PJUD_RUT)  # warm

    if change == "abogado":
        world["causa"].abogado_sysgal_id = 20
        expected_in, expected_out = LUIS_RUT, EDU_RUT
    elif change == "usuario_lawyer":
        world["u_edu"].lawyer_id = db.query(Lawyer).filter_by(rut=LUIS_RUT).one().id
        expected_in, expected_out = LUIS_RUT, EDU_RUT
    else:
        if change == "estado":
            world["causa"].estado = "terminada"
        elif change == "eliminada":
            world["causa"].eliminada = True
        elif change == "case_id":
            world["causa"].case_id = None
        else:
            world["edu"].is_active = False
        expected_in, expected_out = PJUD_RUT, EDU_RUT
    db.commit()

    assert cid in scope(db, expected_in)
    assert cid not in scope(db, expected_out)


def test_new_sysgal_link_invalidates_cached_scope(db, world):
    other = Case(lawyer_id=world["case"].lawyer_id, court_id=world["case"].court_id, rol="C-2-2026",
                 competencia="civil", status="active")
    db.add(other)
    db.commit()
    turn_on(db)
    assert other.id not in scope(db, LUIS_RUT)  # warm
    db.add(SysgalCausa(sysgal_id=2, rol="C-2-2026", rol_norm="C-2-2026", tribunal="J", estado="activa",
                       eliminada=False, abogado_sysgal_id=20, case_id=other.id))
    db.commit()
    assert other.id in scope(db, LUIS_RUT)


def test_bootstrap_owner_loses_a_case_that_sysgal_assigns_to_someone_else(db, world):
    """A case with no litigantes owned (lawyer_id) by PJUD lawyer must leave his scope when Sysgal names another."""
    bare = Case(lawyer_id=world["case"].lawyer_id, court_id=world["case"].court_id, rol="C-3-2026",
                competencia="civil", status="active")
    db.add(bare)
    db.commit()
    assert bare.id in scope(db, PJUD_RUT)
    db.add(SysgalCausa(sysgal_id=3, rol="C-3-2026", rol_norm="C-3-2026", tribunal="J", estado="activa",
                       eliminada=False, abogado_sysgal_id=20, case_id=bare.id))
    db.commit()
    assert bare.id in scope(db, PJUD_RUT)  # switch off: unchanged
    turn_on(db)
    assert bare.id not in scope(db, PJUD_RUT)
    assert bare.id in scope(db, LUIS_RUT)
