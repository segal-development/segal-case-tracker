"""The appeal window a sentencia definitiva starts lasts 10 business days.

Carla Segal confirmed it on 2026-10-23 (art. 189 CPC; Saturday is hábil). The
identifier ``apelacion_5d`` is historical and stays (persisted rows, semáforo
scale, Sysgal contract): only the days, the legal basis and the scale changed.

Covered here:

* the term and the legal basis of the deadline type;
* rows closed ("expired") by the old 5-day rule are re-derived on recompute;
* the appeal stays OPTIONAL: it never makes the case ROJO by itself;
* the semáforo scale covers days 1..10 with no gaps.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.deadlines_config import DeadlineType
from app.core.semaforo_config import Level, SCALE_APELACION_5D
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.deadline_engine import DeadlineEngine
from app.services.deadline_semaforo import assess

APELACION = DeadlineType.APELACION_5D.value

DEMANDA = date(2026, 3, 2)
SENTENCIA_WED = date(2026, 10, 14)
SENTENCIA_FRI = date(2026, 10, 16)
OLD_DUE_WED = add_business_days(SENTENCIA_WED, 5)  # the rule this change replaces


def _freeze(monkeypatch, today: date) -> None:
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: today)


@pytest.fixture
def owner(db):
    lawyer = Lawyer(rut="77777777-7", email="ap@test.com", name="Apelacion")
    court = Court(name="Tribunal Civil", code="TAP10", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _movement(db, case, day: date, stage: str, description: str) -> None:
    db.add(Movement(
        case_id=case.id, stage=stage, procedure="Resolución", description=description,
        movement_date=datetime.combine(day, datetime.min.time()),
    ))
    db.flush()


def _sentencia_case(db, owner, sentencia: date, rol="C-AP-1", side="AB.DDO") -> Case:
    lawyer, court = owner
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", filed_at=datetime(2025, 1, 1), semaforo="verde",
    )
    db.add(case)
    db.flush()
    db.add(CaseLitigante(
        case_id=case.id, participante=side, rut="77777777-7", persona_type="NATURAL",
        nombre="Abogado", natural_key=f"k{case.id}{side}",
    ))
    db.flush()
    _movement(db, case, DEMANDA, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _movement(db, case, date(2026, 4, 1), "Tramitación", "Cita a Audiencia")
    _movement(db, case, sentencia, "Tramitación", "Dicta Sentencia Definitiva")
    return case


def _old_row(db, case, triggered_at: date, **overrides) -> CaseDeadline:
    """A row as the 5-day rule left it: due date 5 días hábiles after the trigger."""
    values = dict(
        case_id=case.id, deadline_type=APELACION, legal_basis="art. 475 CPC",
        due_date=add_business_days(triggered_at, 5), triggered_at=triggered_at,
        status="expired",
    )
    values.update(overrides)
    row = CaseDeadline(**values)
    db.add(row)
    db.flush()
    return row


def _rows(db, case) -> list[CaseDeadline]:
    return (
        db.query(CaseDeadline)
        .filter_by(case_id=case.id, deadline_type=APELACION)
        .order_by(CaseDeadline.id)
        .all()
    )


class TestTermIsTenBusinessDays:
    def test_type_carries_10_days_and_art_189(self) -> None:
        assert DeadlineType.APELACION_5D.value == "apelacion_5d"
        assert DeadlineType.APELACION_5D.dias_habiles == 10
        assert DeadlineType.APELACION_5D.legal_basis == "art. 189 CPC"
        assert DeadlineType.APELACION_5D.is_fatal is True

    def test_wednesday_sentencia_is_due_the_following_monday_week(
        self, db, owner, monkeypatch
    ) -> None:
        # Thu 15 (1), Fri 16 (2), Sat 17 (3, hábil), Mon 19 (4) ... Sat 24 (9), Mon 26 (10).
        _freeze(monkeypatch, date(2026, 10, 15))
        case = _sentencia_case(db, owner, SENTENCIA_WED)

        DeadlineEngine.recompute_case(db, case)

        (row,) = _rows(db, case)
        assert row.due_date == date(2026, 10, 26)
        assert row.legal_basis == "art. 189 CPC"
        assert row.status == "active"

    def test_friday_sentencia_is_due_on_a_wednesday(self, db, owner, monkeypatch) -> None:
        # Sat 17 (1), Mon 19 (2), ... Sat 24 (7), Mon 26 (8), Tue 27 (9), Wed 28 (10).
        _freeze(monkeypatch, date(2026, 10, 17))
        case = _sentencia_case(db, owner, SENTENCIA_FRI)

        DeadlineEngine.recompute_case(db, case)

        (row,) = _rows(db, case)
        assert row.due_date == date(2026, 10, 28)


class TestRowsClosedByTheOldRule:
    def test_expired_row_whose_new_due_is_in_the_future_returns_to_active(
        self, db, owner, monkeypatch
    ) -> None:
        today = OLD_DUE_WED + timedelta(days=1)  # old term over, new one (10-26) open
        _freeze(monkeypatch, today)
        case = _sentencia_case(db, owner, SENTENCIA_WED)
        _old_row(db, case, SENTENCIA_WED)

        DeadlineEngine.recompute_case(db, case)

        (row,) = _rows(db, case)
        assert row.status == "active"
        assert row.due_date == date(2026, 10, 26)
        assert case.next_deadline_at == date(2026, 10, 26)

    def test_expired_row_whose_new_due_also_passed_stays_expired(
        self, db, owner, monkeypatch
    ) -> None:
        _freeze(monkeypatch, date(2026, 11, 20))
        case = _sentencia_case(db, owner, SENTENCIA_WED)
        _old_row(db, case, SENTENCIA_WED)

        DeadlineEngine.recompute_case(db, case)

        (row,) = _rows(db, case)
        assert row.status == "expired"
        assert row.due_date == date(2026, 10, 26)

    def test_superseded_row_of_another_trigger_is_untouched(
        self, db, owner, monkeypatch
    ) -> None:
        _freeze(monkeypatch, OLD_DUE_WED + timedelta(days=1))
        case = _sentencia_case(db, owner, SENTENCIA_WED)
        stale_trigger = date(2026, 9, 1)
        stale = _old_row(db, case, stale_trigger, status="superseded")
        old_due = stale.due_date

        DeadlineEngine.recompute_case(db, case)

        db.refresh(stale)
        assert stale.status == "superseded"
        assert stale.due_date == old_due
        assert stale.legal_basis == "art. 475 CPC"

    @pytest.mark.parametrize("status", ["cumplido", "no_cumplido"])
    def test_audited_row_is_never_touched(self, db, owner, monkeypatch, status) -> None:
        _freeze(monkeypatch, OLD_DUE_WED + timedelta(days=1))
        case = _sentencia_case(db, owner, SENTENCIA_WED)
        audited = _old_row(db, case, SENTENCIA_WED, status=status)
        old_due = audited.due_date

        DeadlineEngine.recompute_case(db, case)

        db.refresh(audited)
        assert audited.status == status
        assert audited.due_date == old_due

    def test_manual_row_is_never_touched(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, OLD_DUE_WED + timedelta(days=1))
        case = _sentencia_case(db, owner, SENTENCIA_WED)
        manual = _old_row(db, case, SENTENCIA_WED, is_manual=True)
        old_due = manual.due_date

        DeadlineEngine.recompute_case(db, case)

        db.refresh(manual)
        assert manual.status == "expired"
        assert manual.due_date == old_due

    def test_auditor_reopened_row_is_never_closed_by_the_engine(
        self, db, owner, monkeypatch
    ) -> None:
        # The auditor API only writes cumplido / no_cumplido / active, so an
        # auditor-touched row can only be "active": the engine must not close it
        # (step 6b skips marked rows) even when both terms are over.
        lawyer, _ = owner
        _freeze(monkeypatch, date(2026, 11, 20))
        case = _sentencia_case(db, owner, SENTENCIA_WED)
        marked = _old_row(
            db, case, SENTENCIA_WED, status="active",
            marked_by=lawyer.id, marked_at=datetime(2026, 10, 22, 12, 0),
        )

        DeadlineEngine.recompute_case(db, case)

        db.refresh(marked)
        assert marked.status == "active"
        assert marked.marked_by == lawyer.id


class TestApelacionStaysOptional:
    @pytest.mark.parametrize(
        "today",
        [date(2026, 10, 15), date(2026, 10, 23), date(2026, 10, 27), date(2026, 12, 1)],
        ids=["day-1", "two-days-left", "day-after", "long-expired"],
    )
    @pytest.mark.parametrize("side", ["AB.DDO", "AB.DTE"])
    def test_never_rojo_outside_the_last_day(
        self, db, owner, monkeypatch, today, side
    ) -> None:
        _freeze(monkeypatch, today)
        case = _sentencia_case(db, owner, SENTENCIA_WED, side=side)

        DeadlineEngine.recompute_case(db, case)

        assert case.semaforo != "rojo"

    def test_expired_window_is_closed_not_red(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, date(2026, 10, 27))
        case = _sentencia_case(db, owner, SENTENCIA_WED)

        DeadlineEngine.recompute_case(db, case)

        (row,) = _rows(db, case)
        assert row.status == "expired"
        assert case.semaforo == "verde"

    def test_last_day_does_not_turn_the_case_rojo(self, db, owner, monkeypatch) -> None:
        # The appeal no longer colours the semáforo, not even on its last day
        # (the firm asked for the semáforo to show everything else). The window
        # itself stays live on the case.
        _freeze(monkeypatch, date(2026, 10, 26))
        case = _sentencia_case(db, owner, SENTENCIA_WED)

        DeadlineEngine.recompute_case(db, case)

        (row,) = _rows(db, case)
        assert row.status == "active"
        assert case.semaforo == "verde"


class TestScaleCoversTheTenDays:
    def test_scale_term_and_bands(self) -> None:
        assert SCALE_APELACION_5D.key == "apelacion_5d"
        assert SCALE_APELACION_5D.term_days == 10
        assert SCALE_APELACION_5D.verde == (1, 5)
        assert SCALE_APELACION_5D.amarillo == (6, 9)
        assert SCALE_APELACION_5D.rojo == (10, 10)

    def test_every_day_1_to_10_has_a_level_and_11_has_none(self) -> None:
        levels = [SCALE_APELACION_5D.level_for_day(d) for d in range(1, 11)]
        assert None not in levels
        assert SCALE_APELACION_5D.level_for_day(11) is None

    @pytest.mark.parametrize(
        "day,expected",
        [(1, Level.VERDE), (5, Level.VERDE), (6, Level.AMARILLO),
         (9, Level.AMARILLO), (10, Level.ROJO)],
    )
    def test_assessment_readings_follow_the_scale(self, day, expected) -> None:
        trigger = SENTENCIA_WED
        # The calendar date of business day ``day`` after the trigger.
        on = add_business_days(trigger, day)
        a = assess(APELACION, trigger, on)
        assert a is not None
        assert a.day == day
        assert a.level == expected
        assert a.readings[0].term_days == 10
        assert a.readings[0].due_date == date(2026, 10, 26)

    def test_over_after_day_10(self) -> None:
        a = assess(APELACION, SENTENCIA_WED, date(2026, 10, 27))
        assert a is not None and a.level is None and a.live_readings == ()
