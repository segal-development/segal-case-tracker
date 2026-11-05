"""The appeal window (apelacion_5d) does not colour the semaforo, per case or on the home panel.

The firm asked for the appeal to stop driving the semaforo so that it shows everything else: an appeal is
a right the firm may or may not exercise, not an obligation. The window stays on the case (deadline row,
next_deadline_at, Agenda); only the colour and the home panel ignore it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.deadlines_config import DeadlineType, ProceduralState
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.deadline_engine import DeadlineEngine
from app.services.semaforo_panel import build_panel

TODAY = date(2026, 6, 15)
RUT = "11111111-1"


@pytest.fixture
def case(db, monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
    lawyer = Lawyer(rut=RUT, email="t@t.cl", name="Test Lawyer", is_active=True)
    court = Court(name="Juzgado Civil", code="C001", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-1-2026", competencia="civil",
        procedure="Ejecutivo", status="active", plaintiff="BANCO X", defendant="PEREZ",
        filed_at=datetime(2024, 1, 1),
    )
    db.add(c)
    db.flush()
    db.add_all([
        CaseLitigante(case_id=c.id, participante="AB.DDO", rut=RUT, persona_type="NATURAL",
                      nombre="Abogado", natural_key="ab1"),
        CaseLitigante(case_id=c.id, participante="DDO.", rut="18765432-1", persona_type="NATURAL",
                      nombre="JUAN PEREZ", natural_key="cl1"),
        Movement(case_id=c.id, stage="Gestión", procedure="",
                 movement_date=datetime.combine(TODAY - timedelta(days=3), datetime.min.time()),
                 description="Resolución"),
    ])
    db.commit()
    return c


def _deadline(db, case, dtype, biz_days, *, status="active"):
    due = add_business_days(TODAY, biz_days)
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=dtype.value, due_date=due,
        triggered_at=TODAY - timedelta(days=2), status=status,
    ))
    db.commit()


def _color(db, case, state=ProceduralState.SENTENCIA):
    return DeadlineEngine._compute_semaforo(db, case.id, state, TODAY)


class TestColorPerCase:
    def test_open_appeal_alone_does_not_colour(self, db, case) -> None:
        _deadline(db, case, DeadlineType.APELACION_5D, 1)
        assert _color(db, case) == "verde"

    def test_open_appeal_does_not_hide_a_later_firm_deadline(self, db, case) -> None:
        _deadline(db, case, DeadlineType.APELACION_5D, 1)
        _deadline(db, case, DeadlineType.TRASLADO_EJECUTANTE_4D, 3)
        assert _color(db, case, ProceduralState.TRASLADO_EJECUTANTE) == "amarillo"

    def test_other_firm_deadlines_still_colour(self, db, case) -> None:
        _deadline(db, case, DeadlineType.TRASLADO_EJECUTANTE_4D, 1)
        assert _color(db, case, ProceduralState.TRASLADO_EJECUTANTE) == "rojo"


class TestHomePanel:
    def test_open_appeal_is_not_on_the_panel(self, db, case) -> None:
        _deadline(db, case, DeadlineType.APELACION_5D, 1)
        assert build_panel(db, TODAY).por_vencer == []

    def test_other_fatal_plazos_stay_on_the_panel(self, db, case) -> None:
        _deadline(db, case, DeadlineType.APELACION_5D, 1)
        _deadline(db, case, DeadlineType.EXCEPCIONES_8D, 1)
        types = [r.deadline_type for r in build_panel(db, TODAY).por_vencer]
        assert DeadlineType.APELACION_5D.value not in types
        assert DeadlineType.EXCEPCIONES_8D.value in types
