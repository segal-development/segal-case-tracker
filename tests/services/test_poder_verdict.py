"""Verdict of the ``acreditar_poder_3d`` plazo (ratification of signature).

``CaseDeadline.status`` is a contract read by Sysgal (``ratificacion.cumplida``)
and the engine writes ``cumplido`` there WITHOUT comparing dates. These tests pin
that the new ``verdict`` tells the truth about timing while ``status`` is left
exactly as it was.

The worst possible output is a false ``cumplido``: it tells a lawyer they are
covered when the fatal plazo has passed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine
from app.services.deadline_verdict import DETECTORS, Verdict, evaluate_deadline

PODER = "acreditar_poder_3d"
PREVIO = "Previo a proveer"
TODAY = date(2026, 6, 16)
TRIGGER = date(2026, 6, 1)  # Monday
DUE = date(2026, 6, 4)  # Thursday: 3 business days later


def _mv(day: date, description: str, *, id: int = 1, procedure: str = "Escrito"):
    return SimpleNamespace(
        id=id, movement_date=datetime.combine(day, datetime.min.time()),
        description=description, procedure=procedure,
    )


def _verdict(movements, *, due=DUE, trigger=TRIGGER, today=TODAY, **kw):
    return evaluate_deadline(PODER, due, movements, today, triggered_at=trigger, **kw)


class TestDetector:
    def test_registered(self) -> None:
        assert PODER in DETECTORS

    def test_on_time_is_cumplido(self) -> None:
        r = _verdict([_mv(DUE, "Acredita Poder")])
        assert r.verdict is Verdict.CUMPLIDO
        assert r.acted_on == DUE

    @pytest.mark.parametrize("text", [
        "Acredita Poder", "Poder acreditado", "Patrocinio y poder", "Delega poder",
    ])
    def test_all_compliance_wordings(self, text) -> None:
        assert _verdict([_mv(DUE, text)]).verdict is Verdict.CUMPLIDO

    def test_returns_the_first_compliance_not_the_last(self) -> None:
        early = _mv(TRIGGER + timedelta(days=1), "Acredita Poder", id=1)
        late = _mv(TRIGGER + timedelta(days=40), "Acredita Poder", id=2)
        for order in ([early, late], [late, early]):
            r = _verdict(order)
            assert r.verdict is Verdict.CUMPLIDO
            assert r.movement_id == 1

    def test_compliance_before_the_resolution_does_not_count(self) -> None:
        # The demand itself often says "Acredita Poder"; it predates the order.
        before = _mv(TRIGGER - timedelta(days=30), "Acredita Poder")
        r = _verdict([before])
        assert r.verdict is not Verdict.CUMPLIDO
        assert r.verdict is Verdict.NO_CUMPLIDO  # nothing after the order, window long gone

    def test_same_day_as_the_resolution_does_not_count(self) -> None:
        # Same rule as find_poder_obligation: indistinguishable from the demand.
        r = _verdict([_mv(TRIGGER, "Acredita Poder")])
        assert r.verdict is not Verdict.CUMPLIDO

    def test_annulled_movement_never_counts(self) -> None:
        r = _verdict([_mv(DUE, "[Nulo] Acredita Poder")])
        assert r.verdict is Verdict.NO_CUMPLIDO

    def test_unrelated_movement_does_not_count(self) -> None:
        assert _verdict([_mv(DUE, "Téngase presente")]).verdict is Verdict.NO_CUMPLIDO

    def test_without_the_resolution_date_there_is_no_verdict(self) -> None:
        # Without the anchor "after the order" cannot be told: never guess.
        r = _verdict([_mv(TRIGGER - timedelta(days=5), "Acredita Poder")], trigger=None)
        assert r.verdict is None


class TestNeverFalseCumplido:
    @pytest.mark.parametrize("days_late", [1, 2, 4, 5, 6, 7, 8, 9, 30, 365])
    def test_compliance_published_after_due_date_is_never_cumplido(self, days_late) -> None:
        r = _verdict([_mv(DUE + timedelta(days=days_late), "Acredita Poder")])
        assert r.verdict is not Verdict.CUMPLIDO
        assert r.verdict is not None

    def test_real_shape_270_group(self) -> None:
        # Real shape of the 270 rows the engine marks "cumplido": a compliance
        # movement exists after the order, but dated past due_date.
        r = _verdict([
            _mv(TRIGGER - timedelta(days=90), "Acredita Poder", id=1),  # with the demand
            _mv(TRIGGER, PREVIO, id=2, procedure="Resolución"),
            _mv(DUE + timedelta(days=12), "Acredita Poder", id=3),
        ])
        assert r.verdict is Verdict.FUERA_DE_PLAZO
        assert r.movement_id == 3


class TestMargin:
    """Two biases stack here, both toward a false ``fuera_de_plazo``: PJUD
    publishes the escrito late, and our due_date counts from the resolution's
    date while the plazo runs from its notification."""

    def test_poder_margin_is_publication_plus_notification_allowance(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "DEADLINE_PUBLICATION_MARGIN_DAYS", 5)
        monkeypatch.setattr(settings, "DEADLINE_PODER_NOTIFICATION_ALLOWANCE_DAYS", 2)
        assert _verdict([_mv(DUE + timedelta(days=7), "Acredita Poder")]).verdict is Verdict.REGISTRO_TARDIO
        assert _verdict([_mv(DUE + timedelta(days=8), "Acredita Poder")]).verdict is Verdict.FUERA_DE_PLAZO

    def test_poder_margin_is_wider_than_the_excepciones_one(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "DEADLINE_PUBLICATION_MARGIN_DAYS", 5)
        monkeypatch.setattr(settings, "DEADLINE_PODER_NOTIFICATION_ALLOWANCE_DAYS", 2)
        day = DUE + timedelta(days=6)
        poder = _verdict([_mv(day, "Acredita Poder")])
        exc = evaluate_deadline(
            "excepciones_8d", DUE, [_mv(day, "Opone excepciones")], TODAY
        )
        assert poder.verdict is Verdict.REGISTRO_TARDIO
        assert exc.verdict is Verdict.FUERA_DE_PLAZO

    def test_excepciones_margin_is_not_affected_by_the_poder_allowance(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "DEADLINE_PODER_NOTIFICATION_ALLOWANCE_DAYS", 30)
        exc = evaluate_deadline(
            "excepciones_8d", DUE, [_mv(DUE + timedelta(days=6), "Opone excepciones")], TODAY
        )
        assert exc.verdict is Verdict.FUERA_DE_PLAZO

    def test_explicit_margin_override_wins(self) -> None:
        r = _verdict([_mv(DUE + timedelta(days=1), "Acredita Poder")], publication_margin_days=0)
        assert r.verdict is Verdict.FUERA_DE_PLAZO

    def test_the_margin_never_turns_a_late_filing_into_cumplido(self) -> None:
        r = _verdict([_mv(DUE + timedelta(days=1), "Acredita Poder")])
        assert r.verdict is Verdict.REGISTRO_TARDIO

    def test_absence_inside_the_margin_is_undetermined_not_no_cumplido(self) -> None:
        # The escrito may be filed on time and simply not published yet.
        today = DUE + timedelta(days=3)
        assert _verdict([], today=today).verdict is None

    def test_absence_beyond_the_margin_is_no_cumplido(self) -> None:
        today = DUE + timedelta(days=30)
        assert _verdict([], today=today).verdict is Verdict.NO_CUMPLIDO

    def test_absence_while_still_in_force_is_undetermined(self) -> None:
        assert _verdict([], today=DUE - timedelta(days=1)).verdict is None


# --- engine integration -----------------------------------------------------


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def case(db) -> Case:
    lawyer = Lawyer(rut="44444444-4", email="pv@test.com", name="Poder Verdict")
    court = Court(name="Tribunal Civil", code="TCPV1", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-PV-1",
        competencia="civil", filed_at=datetime(2025, 1, 1),
    )
    db.add(c)
    db.flush()
    return c


def _add(db, case, day: date, description: str, procedure="Resolución") -> Movement:
    mv = Movement(
        case_id=case.id, stage="Inicio de la Tramitación", description=description,
        procedure=procedure, movement_date=datetime.combine(day, datetime.min.time()),
    )
    db.add(mv)
    db.flush()
    return mv


def _row(db, case) -> CaseDeadline:
    return (
        db.query(CaseDeadline)
        .filter(CaseDeadline.case_id == case.id, CaseDeadline.deadline_type == PODER)
        .one()
    )


class TestEngine:
    def test_on_time_gets_cumplido_verdict(self, db, case) -> None:
        _add(db, case, TRIGGER, PREVIO)
        mv = _add(db, case, DUE, "Acredita Poder", "Escrito")
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case)
        assert row.verdict == "cumplido"
        assert row.verdict_movement_id == mv.id
        assert row.verdict_acted_on == DUE

    def test_late_gets_a_verdict_that_is_not_cumplido_and_status_stays_cumplido(
        self, db, case
    ) -> None:
        _add(db, case, TRIGGER, PREVIO)
        _add(db, case, DUE + timedelta(days=12), "Acredita Poder", "Escrito")
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case)
        # Constraint 1: status is exactly what the engine always wrote (Sysgal reads it).
        assert row.status == "cumplido"
        assert row.verdict == "fuera_de_plazo"

    def test_status_is_identical_with_and_without_the_verdict(self, db, case, monkeypatch) -> None:
        _add(db, case, TRIGGER, PREVIO)
        _add(db, case, DUE + timedelta(days=12), "Acredita Poder", "Escrito")
        DeadlineEngine.recompute_case(db, case)
        with_verdict = _row(db, case).status
        monkeypatch.setattr(
            "app.services.deadline_engine.evaluate_deadline",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        db.query(CaseDeadline).delete()
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        assert _row(db, case).status == with_verdict == "cumplido"

    def test_overdue_row_without_compliance_is_closed_with_a_no_cumplido_verdict(
        self, db, case
    ) -> None:
        _add(db, case, TRIGGER, PREVIO)
        DeadlineEngine.recompute_case(db, case)  # TODAY is 12 days past due, beyond margin
        row = _row(db, case)
        assert row.status == "expired"  # past its term AND margin: no longer live
        assert row.verdict == "no_cumplido"

    def test_existing_engine_written_cumplido_row_gets_its_verdict_on_next_recompute(
        self, db, case
    ) -> None:
        _add(db, case, TRIGGER, PREVIO)
        _add(db, case, DUE + timedelta(days=12), "Acredita Poder", "Escrito")
        db.add(CaseDeadline(
            case_id=case.id, deadline_type=PODER, legal_basis="x", due_date=DUE,
            triggered_at=TRIGGER, status="cumplido",
        ))
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case)
        assert row.status == "cumplido"
        assert row.verdict == "fuera_de_plazo"

    def test_auditor_marked_row_is_never_touched(self, db, case) -> None:
        _add(db, case, TRIGGER, PREVIO)
        _add(db, case, DUE + timedelta(days=12), "Acredita Poder", "Escrito")
        db.add(CaseDeadline(
            case_id=case.id, deadline_type=PODER, legal_basis="x", due_date=DUE,
            triggered_at=TRIGGER, status="cumplido", marked_by=case.lawyer_id,
            marked_at=datetime(2026, 6, 10),
        ))
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case)
        assert row.verdict is None
        assert row.status == "cumplido"

    def test_a_verdict_failure_never_breaks_the_sync(self, db, case, monkeypatch) -> None:
        _add(db, case, TRIGGER, PREVIO)
        _add(db, case, DUE, "Acredita Poder", "Escrito")

        def boom(*a, **k):
            raise RuntimeError("verdict exploded")

        monkeypatch.setattr("app.services.deadline_engine.evaluate_deadline", boom)
        DeadlineEngine.recompute_case(db, case)  # must not raise
        row = _row(db, case)
        assert row.status == "cumplido"
        assert row.verdict is None

    def test_superseded_row_is_left_without_a_verdict(self, db, case) -> None:
        # Judged against the movements of a NEWER resolution it could read a
        # false cumplido, so a replaced row carries no verdict at all.
        _add(db, case, TRIGGER, PREVIO)
        old = CaseDeadline(
            case_id=case.id, deadline_type=PODER, legal_basis="x", due_date=DUE,
            triggered_at=TRIGGER, status="active", verdict="cumplido",
        )
        db.add(old)
        db.flush()
        newer = TRIGGER + timedelta(days=7)
        _add(db, case, newer, PREVIO)
        _add(db, case, newer + timedelta(days=1), "Acredita Poder", "Escrito")
        DeadlineEngine.recompute_case(db, case)
        db.refresh(old)
        assert old.status == "superseded"
        assert old.verdict is None

    def test_a_verdict_failure_does_not_skip_the_rest_of_the_parallel_sync(
        self, db, case, monkeypatch
    ) -> None:
        # The outer safe-fail boundary of the parallel sync would swallow the
        # error but abandon the stale-row supersession that follows it.
        _add(db, case, TRIGGER, PREVIO)
        stale = CaseDeadline(
            case_id=case.id, deadline_type=PODER, legal_basis="x",
            due_date=TRIGGER - timedelta(days=20), triggered_at=TRIGGER - timedelta(days=24),
            status="active",
        )
        db.add(stale)
        db.flush()

        def boom(*a, **k):
            raise RuntimeError("verdict exploded")

        monkeypatch.setattr("app.services.deadline_engine.evaluate_deadline", boom)
        DeadlineEngine.recompute_case(db, case)
        db.refresh(stale)
        assert stale.status == "superseded"

    def test_a_verdict_failure_on_a_classifier_plazo_does_not_break_the_sync(
        self, db, case, monkeypatch
    ) -> None:
        row = CaseDeadline(
            case_id=case.id, deadline_type="excepciones_8d", legal_basis="x",
            due_date=DUE, triggered_at=TRIGGER, status="active",
        )
        db.add(row)
        db.flush()

        def boom(*a, **k):
            raise RuntimeError("verdict exploded")

        monkeypatch.setattr("app.services.deadline_engine.evaluate_deadline", boom)
        DeadlineEngine._record_verdict(row, [], TODAY)  # must not raise
        assert row.verdict is None
