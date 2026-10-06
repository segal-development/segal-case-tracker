"""Backfill of the missing ``excepciones_8d`` plazo rows.

The engine only creates the row if a recompute ran while the case was
NOTIFICADO. A case first scraped AFTER the excepciones were filed never got
one, so the Sysgal endpoint answers ``presentado_sin_ancla``. The backfill
CREATES those rows from the movements. It must do so silently: no alert, no
semaforo change, reversible, idempotent.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.deadlines_config import ProceduralState, actionable_sets
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.backfill_plazos_excepciones import (
    ORIGIN,
    Alcance,
    ejecutar,
    revertir,
)
from app.services.deadline_engine import DeadlineEngine

TODAY = date(2026, 6, 16)
EXC = "excepciones_8d"
NOTIF = "NOTIFICACIÓN DE DEMANDA (Exitosa)"

# Monday. Saturday 7 counts as a business day, so the 8th business day after
# Monday 2026-03-02 is Wednesday 2026-03-11 (it would be Thursday the 12th if
# Saturday were skipped).
MONDAY = date(2026, 3, 2)
DUE_SATURDAY_COUNTS = date(2026, 3, 11)


@pytest.fixture
def owner(db):
    lawyer = Lawyer(rut="33333333-3", email="bf@test.com", name="Backfill Lawyer")
    court = Court(name="Tribunal Civil", code="TCV99", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _case(db, owner, rol, status="active", competencia="civil") -> Case:
    lawyer, court = owner
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia=competencia,
        status=status, filed_at=datetime(2025, 1, 1),
    )
    db.add(c)
    db.flush()
    return c


def _mv(db, case, day, description, stage="Gestión", procedure="Resolución") -> Movement:
    mv = Movement(
        case_id=case.id, stage=stage, description=description, procedure=procedure,
        movement_date=datetime.combine(day, datetime.min.time()),
    )
    db.add(mv)
    db.flush()
    return mv


def _notify(db, case, day=MONDAY) -> Movement:
    return _mv(db, case, day, NOTIF)


def _file_excepciones(db, case, day) -> Movement:
    # Real PJUD filings carry stage "Excepciones": that advances the classifier
    # out of NOTIFICADO, which is exactly why the engine never saw the plazo.
    return _mv(db, case, day, "Opone excepciones", stage="Excepciones", procedure="Escrito")


def _stuck_case(db, owner, rol="C-1-2026", filed_on=date(2026, 3, 10), **kw) -> Case:
    """Notified + excepciones filed, with NO plazo row (the target population)."""
    case = _case(db, owner, rol, **kw)
    _notify(db, case)
    _file_excepciones(db, case, filed_on)
    return case


def _rows(db, case=None) -> list[CaseDeadline]:
    q = db.query(CaseDeadline).filter(CaseDeadline.deadline_type == EXC)
    if case is not None:
        q = q.filter(CaseDeadline.case_id == case.id)
    return q.order_by(CaseDeadline.id).all()


class TestCreation:
    def test_case_with_filing_and_no_row_gets_row_with_anchor_and_due(self, db, owner):
        case = _stuck_case(db, owner)

        ejecutar(db, apply=True, today=TODAY)

        (row,) = _rows(db, case)
        assert row.triggered_at == MONDAY
        assert row.due_date == DUE_SATURDAY_COUNTS
        assert row.legal_basis == "art. 459 CPC"
        assert row.origin == ORIGIN

    def test_saturday_counts_as_business_day(self, db, owner):
        case = _stuck_case(db, owner)

        ejecutar(db, apply=True, today=TODAY)

        (row,) = _rows(db, case)
        assert row.due_date == date(2026, 3, 11)  # not Thursday the 12th
        assert row.due_date != date(2026, 3, 12)

    def test_anchor_uses_diligencia_date_when_present(self, db, owner):
        case = _case(db, owner, "C-2-2026")
        _mv(db, case, date(2026, 3, 6), NOTIF + " Diligencia:02/03/2026 10:00")
        _file_excepciones(db, case, date(2026, 3, 10))

        ejecutar(db, apply=True, today=TODAY)

        (row,) = _rows(db, case)
        assert row.triggered_at == MONDAY

    def test_verdict_is_computed_when_row_is_created(self, db, owner):
        on_time = _stuck_case(db, owner, "C-3-2026", filed_on=DUE_SATURDAY_COUNTS)
        late = _stuck_case(db, owner, "C-4-2026", filed_on=date(2026, 3, 12))

        ejecutar(db, apply=True, today=TODAY)

        (r_on_time,) = _rows(db, on_time)
        (r_late,) = _rows(db, late)
        assert r_on_time.verdict == "cumplido"
        assert r_on_time.verdict_acted_on == DUE_SATURDAY_COUNTS
        assert r_on_time.verdict_movement_id is not None
        assert r_on_time.verdict_computed_at is not None
        assert r_late.verdict == "fuera_de_plazo"

    def test_row_matches_the_one_the_engine_creates(self, db, owner):
        """Same fields as the engine's own row, so the two are interchangeable."""
        engine_case = _case(db, owner, "C-5-2026")
        n = _notify(db, engine_case)
        DeadlineEngine.recompute_case(db, engine_case)
        (engine_row,) = _rows(db, engine_case)

        stuck = _stuck_case(db, owner, "C-6-2026")
        ejecutar(db, apply=True, today=TODAY)
        (row,) = _rows(db, stuck)

        for field in ("deadline_type", "legal_basis", "triggered_at", "due_date", "is_manual"):
            assert getattr(row, field) == getattr(engine_row, field), field
        assert row.source_movement_id is not None
        assert n.id is not None


class TestNoAnchor:
    def test_case_without_determinable_anchor_gets_no_row(self, db, owner):
        case = _case(db, owner, "C-7-2026")
        _file_excepciones(db, case, date(2026, 3, 10))  # no notification at all

        resultado = ejecutar(db, apply=True, today=TODAY)

        assert _rows(db, case) == []
        assert resultado.plan.sin_ancla_total == 1
        assert resultado.plan.a_crear_total == 0


class TestIdempotency:
    def test_second_run_creates_nothing(self, db, owner):
        _stuck_case(db, owner)

        first = ejecutar(db, apply=True, today=TODAY)
        second = ejecutar(db, apply=True, today=TODAY)

        assert len(first.creadas) == 1
        assert second.creadas == []
        assert len(_rows(db)) == 1

    def test_case_that_already_has_a_row_is_not_touched(self, db, owner):
        case = _stuck_case(db, owner)
        existing = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=date(2026, 3, 11),
            triggered_at=date(2026, 3, 2), status="superseded",
        )
        db.add(existing)
        db.flush()

        ejecutar(db, apply=True, today=TODAY)

        (row,) = _rows(db, case)
        assert row.id == existing.id
        assert row.origin is None


class TestDryRun:
    def test_dry_run_writes_nothing(self, db, owner):
        _stuck_case(db, owner, "C-8-2026")
        _stuck_case(db, owner, "C-9-2026", status="archived")
        before = db.query(CaseDeadline).count()

        resultado = ejecutar(db, apply=False, today=TODAY)
        db.rollback()

        assert db.query(CaseDeadline).count() == before == 0
        assert resultado.creadas == []
        # ...yet it reports what it WOULD do, broken down by case status.
        assert resultado.plan.a_crear_total == 2
        assert resultado.plan.a_crear_por_estado == {"active": 1, "archived": 1}

    def test_dry_run_reports_verdicts_and_unanchored(self, db, owner):
        _stuck_case(db, owner, "C-10-2026", filed_on=DUE_SATURDAY_COUNTS)
        _stuck_case(db, owner, "C-11-2026", filed_on=date(2026, 3, 12))
        orphan = _case(db, owner, "C-12-2026", status="closed")
        _file_excepciones(db, orphan, date(2026, 3, 10))

        plan = ejecutar(db, apply=False, today=TODAY).plan

        assert plan.con_presentacion_sin_fila == 3
        assert plan.a_crear_por_veredicto == {"cumplido": 1, "fuera_de_plazo": 1}
        assert plan.sin_ancla_por_estado == {"closed": 1}


class TestScope:
    def test_default_scope_is_active_only(self, db, owner):
        active = _stuck_case(db, owner, "C-13-2026")
        archived = _stuck_case(db, owner, "C-14-2026", status="archived")

        resultado = ejecutar(db, apply=True, today=TODAY)

        assert len(_rows(db, active)) == 1
        assert _rows(db, archived) == []
        assert len(resultado.creadas) == 1

    def test_all_scope_includes_terminated_and_archived(self, db, owner):
        _stuck_case(db, owner, "C-15-2026")
        archived = _stuck_case(db, owner, "C-16-2026", status="archived")
        closed = _stuck_case(db, owner, "C-17-2026", status="closed")

        ejecutar(db, apply=True, alcance=Alcance.TODAS, today=TODAY)

        assert len(_rows(db, archived)) == 1
        assert len(_rows(db, closed)) == 1


class TestSkipsWhatItShouldNot:
    def test_non_civil_case_is_skipped(self, db, owner):
        case = _stuck_case(db, owner, "C-18-2026", competencia="laboral")

        plan = ejecutar(db, apply=True, today=TODAY).plan

        assert _rows(db, case) == []
        assert plan.no_civil == 1

    def test_plazo_still_in_force_for_the_engine_is_left_to_the_engine(self, db, owner):
        """Notified, and the filing is not classified as an opposition stage: the
        engine's next recompute creates the live row itself. Writing a closed
        row here would misstate a plazo that is still running."""
        case = _case(db, owner, "C-19-2026")
        _notify(db, case)
        _mv(db, case, date(2026, 3, 10), "Opone excepciones", procedure="Escrito")

        plan = ejecutar(db, apply=True, today=TODAY).plan

        assert _rows(db, case) == []
        assert plan.plazo_vigente_en_motor == 1


class TestSilence:
    """The point of the whole job: expired fatal plazos must be RECORDED, not
    ANNOUNCED, and must not repaint the portfolio."""

    def test_apply_emits_no_alert_and_does_not_move_the_semaforo(self, db, owner, monkeypatch):
        import app.services.sync_service as sync_service

        def _boom(*a, **k):
            raise AssertionError("backfill must never recompute or alert")

        monkeypatch.setattr(DeadlineEngine, "recompute_case", _boom)
        monkeypatch.setattr(sync_service, "emit_deadline_alerts", _boom)
        monkeypatch.setattr(sync_service, "_fan_out_case_alerts", _boom)

        case = _stuck_case(db, owner)
        case.semaforo = "verde"
        case.next_deadline_at = None
        case.next_deadline_fatal = False
        db.flush()

        ejecutar(db, apply=True, today=TODAY)
        db.refresh(case)

        assert db.query(Alert).count() == 0
        assert case.semaforo == "verde"
        assert case.next_deadline_at is None
        assert case.next_deadline_fatal is False
        assert len(_rows(db, case)) == 1

    def test_created_row_never_feeds_the_semaforo(self, db, owner):
        """An already-expired mandatory plazo, if it counted, would paint ROJO."""
        case = _stuck_case(db, owner, filed_on=date(2026, 3, 20))  # late: verdict set
        ejecutar(db, apply=True, today=TODAY)

        mandatory, actionable = actionable_sets("demandado")[0], actionable_sets("demandado")[1]
        color = DeadlineEngine._compute_semaforo(
            db, case.id, ProceduralState.EXCEPCIONES, TODAY,
            actionable_values=actionable, mandatory_values=mandatory,
        )

        (row,) = _rows(db, case)
        assert row.due_date < TODAY
        assert color != "rojo"
        assert row.status not in ("active", "expired")


class TestRevert:
    def test_revert_removes_exactly_what_was_created(self, db, owner):
        engine_case = _case(db, owner, "C-20-2026")
        _notify(db, engine_case)
        DeadlineEngine.recompute_case(db, engine_case)  # engine-made row: not ours
        _stuck_case(db, owner, "C-21-2026")
        _stuck_case(db, owner, "C-22-2026")
        ejecutar(db, apply=True, today=TODAY)
        assert len(_rows(db)) == 3

        resultado = revertir(db)

        assert resultado.eliminadas == 2
        (left,) = _rows(db)
        assert left.case_id == engine_case.id
        assert left.origin is None

    def test_revert_keeps_rows_touched_since(self, db, owner):
        audited = _stuck_case(db, owner, "C-23-2026")
        plain = _stuck_case(db, owner, "C-24-2026")
        ejecutar(db, apply=True, today=TODAY)
        row = _rows(db, audited)[0]
        row.status = "cumplido"  # an auditor decided this one by hand
        db.flush()

        resultado = revertir(db)

        assert resultado.eliminadas == 1
        assert resultado.conservadas == 1
        assert _rows(db, plain) == []
        assert len(_rows(db, audited)) == 1

    def test_revert_then_apply_restores_the_same_rows(self, db, owner):
        case = _stuck_case(db, owner)
        ejecutar(db, apply=True, today=TODAY)
        revertir(db)
        assert _rows(db, case) == []

        ejecutar(db, apply=True, today=TODAY)

        assert len(_rows(db, case)) == 1
