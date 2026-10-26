"""Prescripción when the demanda was not notified within a year of filing.

Carla Segal (2026-10-23 / 2026-10-26): "si no se logro notificar la demanda
dentro de un año desde su presentación, se puede pedir la prescripción".
The rule is ORed with the previous 1y/3y-from-filed_at rule.
"""

from __future__ import annotations

import types
from datetime import date, datetime

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app as _app  # noqa: F401  (registers all models)

from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine, _compute_prescripcion

TODAY = date(2026, 10, 26)

NOTIF = "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:{d:%d/%m/%Y} 10:00"
REQ = "Requerimiento de Pago (Ficto) Diligencia:{d:%d/%m/%Y} 10:00"


def _filed(**delta) -> datetime:
    d = TODAY - relativedelta(**delta)
    return datetime(d.year, d.month, d.day)


def _case(filed_at, titulo_tipo=None):
    return types.SimpleNamespace(titulo_tipo=titulo_tipo, filed_at=filed_at)


def _mv(day: date, description: str, published_days_after: int = 0):
    published = datetime(day.year, day.month, day.day) + relativedelta(
        days=published_days_after
    )
    return types.SimpleNamespace(
        stage="", procedure="", description=description, movement_date=published
    )


class TestRuleOnPureFunction:
    def test_a_filed_over_a_year_ago_without_notification_is_cumplida(self) -> None:
        filed = _filed(years=1, days=30)
        other = _mv(filed.date() + relativedelta(days=5), "Resolución que provee")
        assert _compute_prescripcion(_case(filed), TODAY, [other]) == (
            True,
            filed.date() + relativedelta(years=1),
        )

    def test_a2_no_movements_at_all_is_unknown_not_marked(self) -> None:
        # PJUD may not have delivered the history: absence of evidence, not
        # evidence that the demanda was never notified.
        filed = _filed(years=1, days=30)
        assert _compute_prescripcion(_case(filed), TODAY, []) == (
            False,
            filed.date() + relativedelta(years=3),
        )

    def test_b_notified_inside_first_year_is_not_marked(self) -> None:
        filed = _filed(years=2)
        notified = _mv(filed.date() + relativedelta(months=3), NOTIF.format(
            d=filed.date() + relativedelta(months=3)))
        assert _compute_prescripcion(_case(filed), TODAY, [notified]) == (
            False,
            filed.date() + relativedelta(years=3),
        )

    def test_c_notified_after_first_year_is_marked(self) -> None:
        filed = _filed(years=2)
        late = filed.date() + relativedelta(years=1, days=1)
        assert _compute_prescripcion(
            _case(filed), TODAY, [_mv(late, NOTIF.format(d=late))]
        ) == (True, filed.date() + relativedelta(years=1))

    def test_d_filed_under_a_year_ago_is_not_marked(self) -> None:
        filed = _filed(months=11)
        assert _compute_prescripcion(_case(filed), TODAY, [])[0] is False

    def test_e_exactly_one_year_today_is_not_marked(self) -> None:
        filed = _filed(years=1)
        assert _compute_prescripcion(_case(filed), TODAY, []) == (
            False,
            filed.date() + relativedelta(years=3),
        )

    def test_f_requerimiento_inside_year_counts_as_notified(self) -> None:
        filed = _filed(years=2)
        day = filed.date() + relativedelta(months=2)
        assert _compute_prescripcion(
            _case(filed), TODAY, [_mv(day, REQ.format(d=day))]
        )[0] is False

    @pytest.mark.parametrize(
        "template",
        ["[Nulo] " + REQ, REQ + " (Paga)"],
        ids=["annulled", "paga"],
    )
    def test_f2_annulled_or_paga_requerimiento_does_not_count(self, template) -> None:
        filed = _filed(years=2)
        day = filed.date() + relativedelta(months=2)
        assert _compute_prescripcion(
            _case(filed), TODAY, [_mv(day, template.format(d=day))]
        ) == (True, filed.date() + relativedelta(years=1))

    def test_diligencia_date_wins_over_late_publication(self) -> None:
        filed = _filed(years=2)
        real = filed.date() + relativedelta(years=1) - relativedelta(days=2)
        mv = _mv(real, NOTIF.format(d=real), published_days_after=5)
        assert _compute_prescripcion(_case(filed), TODAY, [mv])[0] is False

    def test_g_without_filed_at_nothing_is_computable(self) -> None:
        assert _compute_prescripcion(_case(None), TODAY, []) == (False, None)

    def test_h_three_year_rule_still_marks_a_notified_case(self) -> None:
        filed = _filed(years=3, days=10)
        day = filed.date() + relativedelta(months=1)
        assert _compute_prescripcion(
            _case(filed), TODAY, [_mv(day, NOTIF.format(d=day))]
        ) == (True, filed.date() + relativedelta(years=3))

    def test_movements_default_keeps_old_callers_working(self) -> None:
        assert _compute_prescripcion(_case(_filed(months=6)), TODAY)[0] is False


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    session = sessionmaker(bind=eng, autocommit=False, autoflush=False)()
    yield session
    session.close()
    eng.dispose()


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


def _db_case(db, filed_at, procedure="Ejecutivo", movements=()):
    lawyer = Lawyer(rut="33333333-3", email="pnn@test.com", name="PNN Lawyer")
    court = Court(name="Juzgado Civil N", code="N001", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="N-0001-2024",
        competencia="civil",
        procedure=procedure,
        filed_at=filed_at,
    )
    db.add(case)
    db.flush()
    for day, description in movements:
        db.add(
            Movement(
                case_id=case.id,
                stage="",
                procedure="",
                description=description,
                movement_date=datetime(day.year, day.month, day.day),
            )
        )
    db.flush()
    return case


class TestRuleThroughRecompute:
    def test_unnotified_case_is_marked(self, db) -> None:
        filed = _filed(years=1, days=30)
        day = filed.date() + relativedelta(days=5)
        case = _db_case(db, filed, movements=[(day, "Resolución que provee")])
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is True
        assert case.prescripcion_fecha == filed.date() + relativedelta(years=1)

    def test_case_without_movements_is_not_marked(self, db) -> None:
        case = _db_case(db, _filed(years=1, days=30))
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is False

    def test_notified_inside_year_is_not_marked(self, db) -> None:
        filed = _filed(years=2)
        day = filed.date() + relativedelta(months=1)
        case = _db_case(db, filed, movements=[(day, NOTIF.format(d=day))])
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is False

    def test_notified_after_year_is_marked(self, db) -> None:
        filed = _filed(years=2)
        day = filed.date() + relativedelta(years=1, days=5)
        case = _db_case(db, filed, movements=[(day, NOTIF.format(d=day))])
        DeadlineEngine.recompute_case(db, case)
        assert case.prescripcion_cumplida is True

    def test_i_medida_prejudicial_stays_neutral(self, db) -> None:
        case = _db_case(
            db, _filed(years=2), procedure="Medida prejudicial"
        )
        DeadlineEngine.recompute_case(db, case)
        assert not case.prescripcion_cumplida
