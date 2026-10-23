"""An opposition movement published within the plazo proves the escrito was on time.

A movement's date is PJUD's PUBLICATION date, so it can never prove a filing
LATE by itself, but a movement published on day D proves the escrito was filed
on day D or earlier. When a later (re-filed) escrito sits in ``case_escritos``,
the earliest piece of evidence must win. Reproduces C-16984-2025.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.deadlines_config import DeadlineType
from app.models.case_deadline import CaseDeadline
from app.services.business_days import add_business_days
from tests.unit.test_deadline_engine import (
    _add_escrito,
    _add_movement,
    _make_case,
    _recompute,
    _set_side,
)

TODAY = date(2026, 10, 23)
NOTIF = date(2026, 1, 27)
DUE = date(2026, 2, 5)
LATE_ESCRITO = date(2026, 10, 14)
EXC = DeadlineType.EXCEPCIONES_8D.value


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


def test_fixture_dates_match_the_engine_arithmetic():
    assert add_business_days(NOTIF, 8) == DUE


def _seed(db):
    case = _make_case(db)
    _set_side(db, case, "demandado")
    _add_movement(db, case.id, NOTIF, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    return case


def _opone(db, case, day, procedure="Escrito"):
    return _add_movement(
        db, case.id, day, "Gestión", "Opone excepciones", procedure=procedure
    )


def _row(db, case) -> CaseDeadline:
    return db.query(CaseDeadline).filter_by(case_id=case.id, deadline_type=EXC).one()


def _stale_active(db, case) -> CaseDeadline:
    row = CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=NOTIF,
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def _engine_miss(db, case, **extra) -> CaseDeadline:
    """The row the OLD logic left behind for the late escrito."""
    row = CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=NOTIF,
        status="no_cumplido", verdict="fuera_de_plazo",
        verdict_acted_on=LATE_ESCRITO,
        verdict_computed_at=datetime.now(timezone.utc),
        **extra,
    )
    db.add(row)
    case.plazo_fatal_incumplido = True
    db.flush()
    return row


# -- S1: the production case, from a live row ---------------------------------


def test_movement_within_plazo_beats_a_later_escrito(db):
    case = _seed(db)
    mv = _opone(db, case, DUE)
    _add_escrito(db, case.id, LATE_ESCRITO)
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")
    assert row.verdict_acted_on == DUE
    assert row.verdict_movement_id == mv.id
    assert case.plazo_fatal_incumplido is False


def test_movement_after_due_and_late_escrito_stays_a_miss(db):
    case = _seed(db)
    _opone(db, case, date(2026, 2, 6))
    _add_escrito(db, case.id, LATE_ESCRITO)
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("no_cumplido", "fuera_de_plazo")
    assert row.verdict_acted_on == LATE_ESCRITO
    assert row.verdict_movement_id is None
    assert case.plazo_fatal_incumplido is True


def test_on_time_escrito_wins_over_a_later_movement(db):
    case = _seed(db)
    _opone(db, case, DUE)
    _add_escrito(db, case.id, date(2026, 2, 4))
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")
    assert row.verdict_acted_on == date(2026, 2, 4)
    assert row.verdict_movement_id is None


def test_earlier_movement_wins_over_a_later_on_time_escrito(db):
    case = _seed(db)
    mv = _opone(db, case, date(2026, 2, 3))
    _add_escrito(db, case.id, DUE)
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")
    assert row.verdict_acted_on == date(2026, 2, 3)
    assert row.verdict_movement_id == mv.id


# -- S2: movement-only evidence -------------------------------------------------


def test_movement_only_after_due_keeps_the_superseded_closure(db):
    case = _seed(db)
    _opone(db, case, date(2026, 2, 20))
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert row.status == "superseded"
    assert row.verdict == "fuera_de_plazo"  # beyond the publication margin


@pytest.mark.parametrize("procedure", ["Escrito", "Resolución"])
def test_movement_only_within_plazo_is_never_a_miss(db, procedure):
    case = _seed(db)
    mv = _opone(db, case, DUE, procedure=procedure)
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert row.status == "superseded"  # closure unchanged
    assert row.verdict == "cumplido"
    assert row.verdict_acted_on == DUE
    assert row.verdict_movement_id == mv.id
    assert case.plazo_fatal_incumplido is False


def test_annulled_movement_is_not_evidence_of_being_on_time(db):
    case = _seed(db)
    _add_movement(
        db, case.id, DUE, "Gestión", "[Nulo] Opone excepciones", procedure="Escrito"
    )
    _add_escrito(db, case.id, LATE_ESCRITO)
    _stale_active(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("no_cumplido", "fuera_de_plazo")
    assert case.plazo_fatal_incumplido is True


# -- S4: the closed production row is revisited on the next recompute ------------


def test_engine_miss_row_is_fixed_by_the_next_recompute(db):
    case = _seed(db)
    mv = _opone(db, case, DUE)
    _add_escrito(db, case.id, LATE_ESCRITO)
    _engine_miss(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")
    assert row.verdict_acted_on == DUE
    assert row.verdict_movement_id == mv.id
    assert case.plazo_fatal_incumplido is False
    assert db.query(CaseDeadline).filter_by(case_id=case.id).count() == 1


def test_engine_miss_row_stays_a_miss_when_all_evidence_is_late(db):
    case = _seed(db)
    _opone(db, case, date(2026, 2, 6))
    _add_escrito(db, case.id, LATE_ESCRITO)
    _engine_miss(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("no_cumplido", "fuera_de_plazo")
    assert row.verdict_acted_on == LATE_ESCRITO
    assert case.plazo_fatal_incumplido is True


def test_fixed_row_is_stable_on_a_second_recompute(db):
    case = _seed(db)
    _opone(db, case, DUE)
    _add_escrito(db, case.id, LATE_ESCRITO)
    _engine_miss(db, case)

    _recompute(db, case)
    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict, row.verdict_acted_on) == ("cumplido", "cumplido", DUE)
    assert case.plazo_fatal_incumplido is False


# -- Auditor decisions always win -------------------------------------------------


@pytest.mark.parametrize("kind", ["manual", "marked_by", "marked_at_only"])
def test_manual_or_auditor_marked_row_is_untouched(db, kind):
    case = _seed(db)
    _opone(db, case, DUE)
    _add_escrito(db, case.id, LATE_ESCRITO)
    extra = {
        "manual": {"is_manual": True},
        "marked_by": {
            "marked_by": case.lawyer_id,
            "marked_at": datetime(2026, 10, 20),
        },
        "marked_at_only": {"marked_at": datetime(2026, 10, 20)},
    }[kind]
    _engine_miss(db, case, **extra)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("no_cumplido", "fuera_de_plazo")
    assert row.verdict_acted_on == LATE_ESCRITO
    assert case.plazo_fatal_incumplido is True


def test_auditor_no_cumplido_without_a_verdict_is_untouched(db):
    case = _seed(db)
    _opone(db, case, DUE)
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=NOTIF,
        status="no_cumplido",
    ))
    db.flush()

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("no_cumplido", None)


# -- Monotone ---------------------------------------------------------------------


@pytest.mark.parametrize("status", ["cumplido", "superseded"])
def test_cumplido_is_never_turned_into_a_miss(db, status):
    case = _seed(db)
    # The case moved on (the classifier drops the trigger), so a superseded row
    # is a closed one, not a live plazo that gets revived.
    _add_movement(
        db, case.id, date(2026, 1, 30), "Gestión", "Se pronuncia sobre admisibilidad"
    )
    _add_escrito(db, case.id, LATE_ESCRITO)  # late evidence only
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=NOTIF,
        status=status, verdict="cumplido", verdict_acted_on=DUE,
        verdict_computed_at=datetime.now(timezone.utc),
    ))
    db.flush()

    _recompute(db, case)

    row = _row(db, case)
    assert row.status == status
    assert (row.verdict, row.verdict_acted_on) == ("cumplido", DUE)
    assert case.plazo_fatal_incumplido is False
