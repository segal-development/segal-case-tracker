"""The DecisionEngine only emits DEFENSE actions (arts. 459/464 CPC and friends).

`decision_rules` states the firm's side is FIXED to the ejecutado, but PJUD's own
litigante rows show 431 civil cases where a firm lawyer is the ejecutante's abogado
of record (AB.DTE/AP.DTE). Recommending "oponer excepciones" to the creditor's
lawyer is nonsense, so `recompute_case` must withhold the recommendation whenever
the firm's side is CONFIRMED to be demandante.

`_firm_side` returns "demandado" when it cannot tell, so only confirmed
plaintiff-side cases are affected — unverifiable cases keep today's behaviour.
"""

from datetime import date, datetime

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.deadline_engine import DeadlineEngine

LAWYER_RUT = "19456852-5"
TODAY = date(2026, 7, 10)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=LAWYER_RUT, name="Abogado Estudio", role="lawyer")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-SIDE", name="Juzgado Side", region="RM", type="civil")
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _case_with_prescripcion(db, lawyer, court, rol):
    """A case whose state alone would trigger the `oponer_prescripcion` rule."""
    obj = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, status="active",
        competencia="civil", plaintiff="Pérez", defendant="Banco Ejemplo S.A.",
        prescripcion_cumplida=True,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(obj); db.commit(); db.refresh(obj)
    return obj


def _add_abogado(db, case, participante, rut):
    db.add(CaseLitigante(
        case_id=case.id, participante=participante, rut=rut,
        persona_type="NATURAL", nombre="Abogado Estudio",
        natural_key=f"{case.id}-{participante}",
    ))
    db.commit()


class TestDecisionSideGuard:
    def test_defence_side_still_gets_the_recommendation(self, db, lawyer, court):
        # Control: AB.DDO → the firm defends → the defense rule must fire.
        case = _case_with_prescripcion(db, lawyer, court, "C-1-2026")
        _add_abogado(db, case, "AB.DDO", LAWYER_RUT)

        DeadlineEngine._recompute_decision(db, case, TODAY)
        assert case.recommended_action_code == "oponer_prescripcion"

    def test_plaintiff_side_gets_no_defence_recommendation(self, db, lawyer, court):
        # The firm is the ejecutante's abogado → a defense action is wrong.
        case = _case_with_prescripcion(db, lawyer, court, "C-2-2026")
        _add_abogado(db, case, "AB.DTE", LAWYER_RUT)

        DeadlineEngine._recompute_decision(db, case, TODAY)
        assert case.recommended_action_code is None

    def test_ap_dte_also_suppresses(self, db, lawyer, court):
        case = _case_with_prescripcion(db, lawyer, court, "C-3-2026")
        _add_abogado(db, case, "AP.DTE", LAWYER_RUT)

        DeadlineEngine._recompute_decision(db, case, TODAY)
        assert case.recommended_action_code is None

    def test_unverifiable_side_keeps_current_behaviour(self, db, lawyer, court):
        # No abogado litigante at all → _firm_side defaults to "demandado".
        # We must NOT silently drop the recommendation for ~83% of the portfolio.
        case = _case_with_prescripcion(db, lawyer, court, "C-4-2026")

        DeadlineEngine._recompute_decision(db, case, TODAY)
        assert case.recommended_action_code == "oponer_prescripcion"
