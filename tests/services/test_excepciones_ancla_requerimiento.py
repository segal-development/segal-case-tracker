"""excepciones_8d runs from the REQUERIMIENTO DE PAGO, not from the cédula.

Carla Segal (2026-10-23): "LA FECHA COMIENZA A CORRER DESDE EL REQUERIMIENTO DE
PAGO NO DESDE LA ENTREGA DE LA CEDULA". The anchor is the ``Diligencia:`` date
of the earliest valid "Requerimiento de Pago" movement; the row identity
(triggered_at = notification) is unchanged, only the due date moves.
Reproduces C-11187-2026.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.deadlines_config import DeadlineType
from app.models.case_deadline import CaseDeadline
from app.models.movement import Movement
from app.services.business_days import add_business_days
from tests.services.test_excepciones_movement_on_time import _opone, _row
from tests.unit.test_deadline_engine import (
    _add_escrito,
    _add_movement,
    _make_case,
    _recompute,
    _set_side,
)

EXC = DeadlineType.EXCEPCIONES_8D.value
NOTIF = date(2026, 9, 15)  # Diligencia: of the notification
NOTIF_PUBLISHED = date(2026, 9, 16)
OLD_DUE = date(2026, 9, 26)  # 8 business days from NOTIF (18 and 19 are holidays)
LIVE_TODAY = date(2026, 9, 17)  # every plazo is still running
PROD_TODAY = date(2026, 10, 23)


def _due(anchor: date) -> date:
    return add_business_days(anchor, 8)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: PROD_TODAY)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: LIVE_TODAY)


def _seed(db, notif=NOTIF):
    case = _make_case(db)
    _set_side(db, case, "demandado")
    _add_movement(
        db, case.id, NOTIF_PUBLISHED, "Gestión",
        f"NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:{notif:%d/%m/%Y} 10:00",
    )
    return case


def _req(db, case, day, form="(Ficto)", stage="Exhorto"):
    """A requerimiento movement with a ``Diligencia:`` date, published a day later."""
    return _add_movement(
        db, case.id, day + timedelta(days=1), stage,
        f"Requerimiento de Pago {form} Diligencia:{day:%d/%m/%Y} 08:33".replace("  ", " "),
    )


def _notification_id(db, case) -> int:
    """The movement the engine stores as ``source_movement_id`` of the row."""
    return (
        db.query(Movement)
        .filter(Movement.case_id == case.id, Movement.description.like("NOTIFICACIÓN%"))
        .one()
        .id
    )


def _rows(db, case):
    return db.query(CaseDeadline).filter_by(case_id=case.id, deadline_type=EXC).all()


def _miss(db, case, **extra):
    """The row the OLD anchor left behind: late against the cédula's due date."""
    row = CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=OLD_DUE, triggered_at=NOTIF,
        source_movement_id=_notification_id(db, case),
        status="no_cumplido", verdict="fuera_de_plazo",
        verdict_acted_on=date(2026, 9, 30),
        verdict_computed_at=datetime.now(timezone.utc),
        **extra,
    )
    db.add(row)
    case.plazo_fatal_incumplido = True
    db.flush()
    return row


# -- (a) the production case ----------------------------------------------------


def test_production_case_late_miss_becomes_cumplido_against_the_requerimiento(db):
    case = _seed(db)
    mv = _req(db, case, date(2026, 9, 21))
    _add_escrito(db, case.id, date(2026, 9, 30))
    _miss(db, case)
    assert mv.stage == "Exhorto"

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")
    assert row.verdict_acted_on == date(2026, 9, 30)
    assert row.triggered_at == NOTIF
    assert row.due_date == date(2026, 9, 30)
    assert case.plazo_fatal_incumplido is False
    assert len(_rows(db, case)) == 1


def test_production_case_is_stable_on_a_second_recompute(db):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    _add_escrito(db, case.id, date(2026, 9, 30))
    _miss(db, case)

    _recompute(db, case)
    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict, row.due_date) == ("cumplido", "cumplido", date(2026, 9, 30))
    assert case.plazo_fatal_incumplido is False
    assert len(_rows(db, case)) == 1


# -- (j) late even against the NEW due ---------------------------------------------


def test_escrito_late_even_against_the_new_due_stays_a_miss(db):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    _add_escrito(db, case.id, date(2026, 10, 1))
    _miss(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("no_cumplido", "fuera_de_plazo")
    assert row.due_date == date(2026, 9, 30)  # the miss is shown against the real due
    assert case.plazo_fatal_incumplido is True
    assert len(_rows(db, case)) == 1


# -- (b)-(h) which requerimiento anchors the plazo ------------------------------------


def test_no_requerimiento_keeps_the_notification_anchor(db, live):
    case = _seed(db)

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert (row.status, row.triggered_at, row.due_date) == ("active", NOTIF, OLD_DUE)


def test_requerimiento_the_same_day_gives_the_same_due(db, live):
    case = _seed(db)
    _req(db, case, NOTIF)

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == OLD_DUE == _due(NOTIF)


def test_requerimiento_before_the_notification_is_ignored(db, live):
    case = _seed(db)
    _req(db, case, date(2026, 9, 14))

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == OLD_DUE


def test_requerimiento_exactly_30_days_after_is_used(db, live):
    case = _seed(db)
    day = NOTIF + timedelta(days=30)
    _req(db, case, day)

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == _due(day) != OLD_DUE


def test_requerimiento_31_days_after_is_ignored(db, live):
    case = _seed(db)
    _req(db, case, NOTIF + timedelta(days=31))

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == OLD_DUE


@pytest.mark.parametrize("form", ["(Personal)", "(Realizada)", ""])
def test_every_requerimiento_variant_with_a_diligencia_anchors(db, live, form):
    case = _seed(db)
    day = date(2026, 9, 22)
    _req(db, case, day, form=form)

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == _due(day)


def test_requerimiento_paga_is_ignored(db, live):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21), form="(Paga)")

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == OLD_DUE


def test_annulled_requerimiento_is_ignored(db, live):
    case = _seed(db)
    _add_movement(
        db, case.id, date(2026, 9, 22), "Exhorto",
        "[Nulo] Requerimiento de Pago (Ficto) Diligencia:21/09/2026 08:33",
    )

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == OLD_DUE


def test_requerimiento_without_a_diligencia_is_never_used(db, live):
    case = _seed(db)
    # Its date is a publication date and may be later than the real act.
    _add_movement(db, case.id, date(2026, 9, 25), "Exhorto", "Requerimiento de pago")

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == OLD_DUE


def test_the_earliest_valid_requerimiento_wins(db, live):
    case = _seed(db)
    _req(db, case, date(2026, 9, 24), form="(Personal)")
    _req(db, case, date(2026, 9, 21))
    _req(db, case, date(2026, 9, 14), form="(Ficto)")  # before the anchor: skipped
    _req(db, case, date(2026, 9, 20), form="(Paga)")  # paid: skipped
    _req(db, case, NOTIF + timedelta(days=141))  # atypical outlier: skipped

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert row.due_date == _due(date(2026, 9, 21))


# -- (i) a live row ------------------------------------------------------------------


def test_live_row_moves_its_due_without_a_duplicate_and_is_idempotent(db, live):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=OLD_DUE, triggered_at=NOTIF,
        source_movement_id=_notification_id(db, case), status="active",
    ))
    db.flush()

    _recompute(db, case)
    _recompute(db, case)

    (row,) = _rows(db, case)
    assert (row.status, row.triggered_at, row.due_date) == ("active", NOTIF, date(2026, 9, 30))


def test_opposition_within_the_new_due_closes_a_live_row_as_cumplido(db, live):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    _add_escrito(db, case.id, date(2026, 9, 29))
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=OLD_DUE, triggered_at=NOTIF,
        source_movement_id=_notification_id(db, case), status="active",
    ))
    db.flush()

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")


def test_opposition_movement_within_the_new_due_corrects_the_miss(db):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    mv = _opone(db, case, date(2026, 9, 29))
    _add_escrito(db, case.id, date(2026, 10, 14))
    _miss(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict) == ("cumplido", "cumplido")
    assert row.verdict_movement_id == mv.id
    assert case.plazo_fatal_incumplido is False


# -- (k) auditor decisions always win ------------------------------------------------


@pytest.mark.parametrize("kind", ["manual", "marked_by", "marked_at_only"])
def test_manual_or_auditor_marked_row_is_untouched(db, kind):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    _add_escrito(db, case.id, date(2026, 9, 30))
    extra = {
        "manual": {"is_manual": True},
        "marked_by": {"marked_by": case.lawyer_id, "marked_at": datetime(2026, 10, 20)},
        "marked_at_only": {"marked_at": datetime(2026, 10, 20)},
    }[kind]
    _miss(db, case, **extra)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict, row.due_date) == ("no_cumplido", "fuera_de_plazo", OLD_DUE)
    assert case.plazo_fatal_incumplido is True


def test_auditor_cumplido_row_keeps_its_due_date(db):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))
    db.add(CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=OLD_DUE, triggered_at=NOTIF,
        status="cumplido", marked_by=case.lawyer_id, marked_at=datetime(2026, 10, 1),
    ))
    db.flush()

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.due_date) == ("cumplido", OLD_DUE)
    assert len(_rows(db, case)) == 1


# -- (l) the production shapes without a requerimiento do not change --------------------


def test_miss_without_a_requerimiento_stays_a_miss(db):
    case = _seed(db)
    _add_escrito(db, case.id, date(2026, 9, 30))
    _miss(db, case)

    _recompute(db, case)

    row = _row(db, case)
    assert (row.status, row.verdict, row.due_date) == ("no_cumplido", "fuera_de_plazo", OLD_DUE)
    assert case.plazo_fatal_incumplido is True
    assert len(_rows(db, case)) == 1


def test_fresh_case_creates_one_row_anchored_on_the_requerimiento(db, live):
    case = _seed(db)
    _req(db, case, date(2026, 9, 21))

    _recompute(db, case)

    (row,) = _rows(db, case)
    assert (row.status, row.triggered_at, row.due_date) == ("active", NOTIF, date(2026, 9, 30))
