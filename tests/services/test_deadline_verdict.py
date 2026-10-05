"""Verdict of an excepciones_8d plazo: was the obligation met, independent of
the plazo's lifecycle ``status``.

The engine answers "which plazo is current?"; the business asks "was it met on
time?". These tests pin that the answer is persisted and survives the plazo
becoming ``superseded`` (step 5 / 5b of the engine).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.deadline_engine import DeadlineEngine
from app.services.deadline_verdict import (
    DETECTORS,
    Verdict,
    evaluate_deadline,
)

TODAY = date(2026, 6, 16)
EXC = "excepciones_8d"
NOTIF_DESC = "NOTIFICACIÓN DE DEMANDA (Exitosa)"


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def case(db) -> Case:
    lawyer = Lawyer(rut="33333333-3", email="v@test.com", name="Verdict Lawyer")
    court = Court(name="Tribunal Civil", code="TCV01", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol="C-VERD-1",
        competencia="civil", filed_at=datetime(2025, 1, 1),
    )
    db.add(c)
    db.flush()
    return c


def _add(
    db, case: Case, day: date, description: str,
    stage: str = "Gestión", procedure: str = "Resolución",
) -> Movement:
    mv = Movement(
        case_id=case.id, stage=stage, description=description, procedure=procedure,
        movement_date=datetime.combine(day, datetime.min.time()),
    )
    db.add(mv)
    db.flush()
    return mv


def _exc_rows(db, case: Case) -> list[CaseDeadline]:
    return (
        db.query(CaseDeadline)
        .filter(CaseDeadline.case_id == case.id, CaseDeadline.deadline_type == EXC)
        .order_by(CaseDeadline.id)
        .all()
    )


def _notify(db, case: Case, days_ago: int) -> tuple[Movement, date]:
    """Notify the demanda `days_ago` before TODAY; return it and the due date."""
    notified = TODAY - timedelta(days=days_ago)
    mv = _add(db, case, notified, NOTIF_DESC)
    return mv, add_business_days(notified, 8)


def _notify_and_sync(db, case: Case, days_ago: int) -> date:
    """Notify, then run the engine once so the plazo row exists, as it does in
    production (the sync recomputes while the case is still NOTIFICADO). A case
    first scraped AFTER it already presented never gets an excepciones row."""
    _, due = _notify(db, case, days_ago)
    DeadlineEngine.recompute_case(db, case)
    return due


def _present(db, case: Case, day: date, **kw) -> Movement:
    kw.setdefault("stage", "Excepciones")
    kw.setdefault("procedure", "Escrito")
    return _add(db, case, day, kw.pop("description", "Opone excepciones"), **kw)


class TestVerdictOnClose:
    def test_presentation_before_due_date_is_cumplido_with_proof(self, db, case) -> None:
        due = _notify_and_sync(db, case, 30)
        proof = _present(db, case, due - timedelta(days=2))
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.verdict == Verdict.CUMPLIDO.value
        assert row.verdict_movement_id == proof.id
        assert row.verdict_acted_on == due - timedelta(days=2)
        assert row.verdict_computed_at is not None

    def test_presentation_on_the_due_date_is_still_cumplido(self, db, case) -> None:
        due = _notify_and_sync(db, case, 30)
        _present(db, case, due)
        DeadlineEngine.recompute_case(db, case)
        (row,) = _exc_rows(db, case)
        assert row.verdict == Verdict.CUMPLIDO.value

    def test_presentation_after_due_date_is_fuera_de_plazo(self, db, case) -> None:
        due = _notify_and_sync(db, case, 30)
        proof = _present(db, case, due + timedelta(days=3))
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.verdict == Verdict.FUERA_DE_PLAZO.value
        assert row.verdict_movement_id == proof.id

    def test_expired_without_presentation_is_no_cumplido(self, db, case) -> None:
        _notify(db, case, 30)
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.status == "active"  # overdue but nothing moved on: still ROJO
        assert row.verdict == Verdict.NO_CUMPLIDO.value
        assert row.verdict_movement_id is None

    def test_current_plazo_is_sin_determinar_not_no_cumplido(self, db, case) -> None:
        _notify(db, case, 2)
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.status == "active"
        assert row.due_date >= TODAY
        assert row.verdict is None
        assert row.verdict_computed_at is None


class TestVerdictSurvivesSupersede:
    def test_verdict_survives_step_5_supersede(self, db, case) -> None:
        """Presentation advances the state; step 5 supersedes the plazo."""
        due = _notify_and_sync(db, case, 30)
        _present(db, case, due - timedelta(days=2))
        DeadlineEngine.recompute_case(db, case)
        DeadlineEngine.recompute_case(db, case)  # idempotent across runs

        (row,) = _exc_rows(db, case)
        assert row.status == "superseded"
        assert row.verdict == Verdict.CUMPLIDO.value

    def test_missed_plazo_superseded_by_5b_keeps_no_cumplido(self, db, case) -> None:
        """Later unrelated activity supersedes an overdue plazo (step 5b): the
        miss must not disappear with it."""
        _, due = _notify(db, case, 30)
        _add(db, case, due + timedelta(days=2), "Téngase presente", stage="Gestión")
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.status == "superseded"
        assert row.verdict == Verdict.NO_CUMPLIDO.value

    def test_superseded_before_due_without_presentation_stays_sin_determinar(
        self, db, case
    ) -> None:
        _notify(db, case, 2)
        _add(db, case, TODAY - timedelta(days=1), "Téngase presente")
        DeadlineEngine.recompute_case(db, case)
        for row in _exc_rows(db, case):
            if row.status == "superseded":
                assert row.verdict is None


class TestWhatCountsAsPresentation:
    def test_opone_excepciones_as_resolucion_is_the_courts_ruling_not_a_filing(
        self, db, case
    ) -> None:
        due = _notify_and_sync(db, case, 30)
        _present(db, case, due - timedelta(days=2), procedure="Resolución")
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.verdict == Verdict.NO_CUMPLIDO.value
        assert row.verdict_movement_id is None

    def test_nulo_presentation_does_not_count(self, db, case) -> None:
        due = _notify_and_sync(db, case, 30)
        _present(db, case, due - timedelta(days=2), description="[Nulo] Opone excepciones")
        DeadlineEngine.recompute_case(db, case)

        (row,) = _exc_rows(db, case)
        assert row.verdict == Verdict.NO_CUMPLIDO.value

    def test_missing_procedure_does_not_count(self, db, case) -> None:
        mv = _add(db, case, TODAY, "Opone excepciones", procedure="")
        assert DETECTORS[EXC]([mv]) is None


class TestAuditorMarksWin:
    def test_audited_row_is_not_given_a_verdict(self, db, case) -> None:
        notified_mv, due = _notify(db, case, 30)
        _present(db, case, due - timedelta(days=2))
        audited = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=due,
            triggered_at=TODAY - timedelta(days=30), status="no_cumplido",
            source_movement_id=notified_mv.id,
        )
        db.add(audited)
        db.flush()

        DeadlineEngine.recompute_case(db, case)
        db.refresh(audited)
        assert audited.status == "no_cumplido"
        assert audited.verdict is None

    def test_manual_row_is_not_given_a_verdict(self, db, case) -> None:
        notified_mv, due = _notify(db, case, 30)
        manual = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=due,
            triggered_at=TODAY - timedelta(days=30), status="active", is_manual=True,
            source_movement_id=notified_mv.id,
        )
        db.add(manual)
        db.flush()

        DeadlineEngine.recompute_case(db, case)
        db.refresh(manual)
        assert manual.verdict is None


class TestDueDateChanges:
    def test_reanchored_plazo_verdict_follows_the_new_row(self, db, case) -> None:
        """#315 moved the anchor to the diligencia date: the engine creates a
        new row and supersedes the old one. The old row (stale due_date) must
        not carry a verdict; the new one is judged against its own due_date."""
        published = TODAY - timedelta(days=30)
        diligencia = published - timedelta(days=4)
        desc = f"{NOTIF_DESC} Diligencia:{diligencia:%d/%m/%Y} 10:00"
        mv = _add(db, case, published, desc)
        old_due = add_business_days(published, 8)
        new_due = add_business_days(diligencia, 8)
        assert new_due < old_due
        old = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=old_due,
            triggered_at=published, status="active", source_movement_id=mv.id,
        )
        db.add(old)
        db.flush()

        DeadlineEngine.recompute_case(db, case)
        db.refresh(old)
        new = [r for r in _exc_rows(db, case) if r.triggered_at == diligencia][0]
        assert old.status == "superseded"
        assert old.verdict is None
        assert new.verdict == Verdict.NO_CUMPLIDO.value

    def test_stale_due_date_is_recomputed_from_the_source_movement(self, db, case) -> None:
        """Presented on the OLD due date: on time under the old anchor, late
        under the diligencia anchor. The row is closed in the same run that
        advances the state, so the verdict must use the fresh anchor."""
        published = TODAY - timedelta(days=30)
        diligencia = published - timedelta(days=4)
        desc = f"{NOTIF_DESC} Diligencia:{diligencia:%d/%m/%Y} 10:00"
        mv = _add(db, case, published, desc)
        old_due = add_business_days(published, 8)
        new_due = add_business_days(diligencia, 8)
        _present(db, case, old_due)
        old = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=old_due,
            triggered_at=published, status="active", source_movement_id=mv.id,
        )
        db.add(old)
        db.flush()
        assert old_due > new_due

        DeadlineEngine.recompute_case(db, case)
        db.refresh(old)
        assert old.status == "superseded"
        assert old.verdict == Verdict.FUERA_DE_PLAZO.value

    def test_reopened_row_drops_its_old_verdict(self, db, case) -> None:
        """A superseded row revived by the upsert (same triggered_at) is a live
        plazo again: a verdict from the earlier closure must not linger."""
        _, due = _notify(db, case, 2)
        stale = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=due,
            triggered_at=TODAY - timedelta(days=2), status="superseded",
            verdict=Verdict.NO_CUMPLIDO.value, verdict_computed_at=datetime(2026, 1, 1),
        )
        db.add(stale)
        db.flush()

        DeadlineEngine.recompute_case(db, case)
        db.refresh(stale)
        assert stale.status == "active"
        assert stale.verdict is None
        assert stale.verdict_computed_at is None


class TestEvaluator:
    def test_type_without_detector_is_sin_determinar_even_when_overdue(self) -> None:
        assert "traslado_ejecutante_4d" not in DETECTORS
        result = evaluate_deadline(
            "traslado_ejecutante_4d", TODAY - timedelta(days=30), [], TODAY
        )
        assert result.verdict is None

    def test_plazo_in_force_without_presentation_is_sin_determinar(self) -> None:
        in_force = evaluate_deadline(EXC, TODAY + timedelta(days=3), [], TODAY)
        assert in_force.verdict is None
        # The due date itself is still inside the window.
        assert evaluate_deadline(EXC, TODAY, [], TODAY).verdict is None

    def test_presented_without_anchor_is_its_own_verdict(self) -> None:
        mv = Movement(
            id=7, case_id=1, stage="Excepciones", procedure="Escrito",
            description="Opone excepciones", movement_date=datetime(2026, 1, 5),
        )
        presented = evaluate_deadline(EXC, None, [mv], TODAY)
        assert presented.verdict is Verdict.PRESENTADO_SIN_ANCLA
        assert presented.movement_id == 7
        assert presented.verdict not in (Verdict.CUMPLIDO, Verdict.NO_CUMPLIDO)
        # No presentation and no anchor: nothing can be said.
        assert evaluate_deadline(EXC, None, [], TODAY).verdict is None

    def test_earliest_valid_presentation_is_the_proof(self) -> None:
        early = Movement(
            id=1, case_id=1, procedure="Escrito", description="Opone excepciones",
            movement_date=datetime(2026, 5, 1),
        )
        late = Movement(
            id=2, case_id=1, procedure="Escrito", description="Opone excepciones",
            movement_date=datetime(2026, 5, 20),
        )
        result = evaluate_deadline(EXC, date(2026, 5, 10), [late, early], TODAY)
        assert result.verdict is Verdict.CUMPLIDO
        assert result.movement_id == 1
