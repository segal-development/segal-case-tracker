"""Re-evaluation of the persisted verdict of CLOSED plazo rows.

A verdict is persisted when a plazo closes. If the rule changes afterwards (e.g.
the publication margin), those rows keep the old answer. The job re-runs
``evaluate_deadline`` over closed rows. It must be dry-run by default, leave
auditor decisions and live rows alone, be idempotent and stay silent (no alert,
no semaforo change).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine
from app.services.recalcular_veredictos import ejecutar

TODAY = date(2026, 6, 16)
DUE = date(2026, 3, 11)
EXC = "excepciones_8d"
STALE_AT = datetime(2026, 4, 1, 12, 0)


@pytest.fixture
def owner(db):
    lawyer = Lawyer(rut="44444444-4", email="rv@test.com", name="Reverdict Lawyer")
    court = Court(name="Tribunal Civil", code="TCV98", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _closed_row(
    db, owner, rol, *, filed_on, verdict="fuera_de_plazo", status="superseded",
    is_manual=False, marked_by=None, marked_at=None,
):
    """Case + filing movement + a plazo row carrying a (stale) persisted verdict."""
    lawyer, court = owner
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", filed_at=datetime(2025, 1, 1), semaforo="verde",
    )
    db.add(case)
    db.flush()
    mv = Movement(
        case_id=case.id, stage="Excepciones", description="Opone excepciones",
        procedure="Escrito", movement_date=datetime.combine(filed_on, datetime.min.time()),
    )
    db.add(mv)
    db.flush()
    row = CaseDeadline(
        case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=date(2026, 3, 2),
        status=status, verdict=verdict, verdict_movement_id=mv.id,
        verdict_acted_on=filed_on, verdict_computed_at=STALE_AT,
        is_manual=is_manual, marked_by=marked_by, marked_at=marked_at,
    )
    db.add(row)
    db.flush()
    return case, row


class TestWhichRowsChange:
    def test_fuera_de_plazo_inside_margin_becomes_registro_tardio(self, db, owner):
        _, row = _closed_row(db, owner, "C-1-2026", filed_on=DUE + timedelta(days=3))

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "registro_tardio"
        assert row.verdict_computed_at != STALE_AT
        assert resultado.transiciones == {("fuera_de_plazo", "registro_tardio"): 1}

    def test_fuera_de_plazo_beyond_margin_is_untouched(self, db, owner):
        _, row = _closed_row(db, owner, "C-2-2026", filed_on=DUE + timedelta(days=6))

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "fuera_de_plazo"
        assert row.verdict_computed_at == STALE_AT
        assert resultado.cambios == []

    def test_expired_rows_are_closed_too(self, db, owner):
        _, row = _closed_row(
            db, owner, "C-3-2026", filed_on=DUE + timedelta(days=2), status="expired"
        )

        ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "registro_tardio"
        assert row.status == "expired"  # status is never touched

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"status": "cumplido"},
            {"status": "no_cumplido"},
            {"is_manual": True},
            {"marked_by": 1},  # replaced by a real lawyer id below
        ],
        ids=["cumplido", "no_cumplido", "is_manual", "marked_by"],
    )
    def test_auditor_marked_row_is_untouched_and_reported_apart(self, db, owner, kwargs):
        if "marked_by" in kwargs:
            kwargs = {"marked_by": owner[0].id, "marked_at": STALE_AT}
        _, row = _closed_row(
            db, owner, "C-4-2026", filed_on=DUE + timedelta(days=3), **kwargs
        )

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "fuera_de_plazo"
        assert row.verdict_computed_at == STALE_AT
        assert resultado.cambios == []
        assert resultado.protegidas == 1
        assert resultado.protegidas_que_cambiarian == {("fuera_de_plazo", "registro_tardio"): 1}

    def test_live_row_is_untouched(self, db, owner):
        _, row = _closed_row(
            db, owner, "C-5-2026", filed_on=DUE + timedelta(days=3), status="active"
        )

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "fuera_de_plazo"
        assert row.verdict_computed_at == STALE_AT
        assert resultado.cambios == []
        assert resultado.vigentes == 1

    def test_closed_row_without_verdict_is_untouched(self, db, owner):
        """Superseded without verdict = re-anchored (stale due_date): not ours to judge."""
        _, row = _closed_row(
            db, owner, "C-6-2026", filed_on=DUE + timedelta(days=3), verdict=None
        )

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict is None
        assert resultado.cambios == []

    def test_margin_override_zero_restores_the_pre_margin_verdict(self, db, owner):
        """The undo: margin 0 is the old rule, so re-running with it reverts."""
        _, row = _closed_row(
            db, owner, "C-7-2026", filed_on=DUE + timedelta(days=3), verdict="registro_tardio"
        )

        ejecutar(db, apply=True, today=TODAY, publication_margin_days=0)

        db.refresh(row)
        assert row.verdict == "fuera_de_plazo"


class TestDryRun:
    def test_dry_run_writes_nothing_but_reports_the_transitions(self, db, owner):
        rows = [
            _closed_row(db, owner, f"C-1{i}-2026", filed_on=DUE + timedelta(days=3))[1]
            for i in range(3)
        ]
        before = [(r.id, r.verdict, r.verdict_computed_at) for r in rows]
        count_before = db.query(CaseDeadline).count()

        resultado = ejecutar(db, today=TODAY)  # apply defaults to False
        db.expire_all()

        after = [(r.id, r.verdict, r.verdict_computed_at) for r in db.query(CaseDeadline).order_by(CaseDeadline.id)]
        assert after == before
        assert db.query(CaseDeadline).count() == count_before
        assert resultado.apply is False
        assert resultado.transiciones == {("fuera_de_plazo", "registro_tardio"): 3}


class TestIdempotence:
    def test_second_run_changes_nothing(self, db, owner):
        _closed_row(db, owner, "C-20-2026", filed_on=DUE + timedelta(days=3))
        _closed_row(db, owner, "C-21-2026", filed_on=DUE + timedelta(days=9))

        first = ejecutar(db, apply=True, today=TODAY)
        snapshot = [
            (r.id, r.verdict, r.verdict_movement_id, r.verdict_acted_on, r.verdict_computed_at)
            for r in db.query(CaseDeadline).order_by(CaseDeadline.id)
        ]
        second = ejecutar(db, apply=True, today=TODAY)
        db.expire_all()

        assert len(first.cambios) == 1
        assert second.cambios == []
        assert snapshot == [
            (r.id, r.verdict, r.verdict_movement_id, r.verdict_acted_on, r.verdict_computed_at)
            for r in db.query(CaseDeadline).order_by(CaseDeadline.id)
        ]


class TestSilence:
    def test_apply_emits_no_alert_and_does_not_move_the_semaforo(self, db, owner, monkeypatch):
        import app.services.sync_service as sync_service

        def _boom(*a, **k):
            raise AssertionError("re-evaluating verdicts must never recompute or alert")

        monkeypatch.setattr(DeadlineEngine, "recompute_case", _boom)
        monkeypatch.setattr(sync_service, "emit_deadline_alerts", _boom)
        monkeypatch.setattr(sync_service, "_fan_out_case_alerts", _boom)

        case, row = _closed_row(
            db, owner, "C-30-2026", filed_on=DUE + timedelta(days=3), status="expired"
        )
        case.semaforo = "verde"
        case.next_deadline_at = None
        case.next_deadline_fatal = False
        db.flush()

        resultado = ejecutar(db, apply=True, today=TODAY)
        db.refresh(case)
        db.refresh(row)

        assert len(resultado.cambios) == 1 and row.verdict == "registro_tardio"
        assert db.query(Alert).count() == 0
        assert case.semaforo == "verde"
        assert case.next_deadline_at is None
        assert case.next_deadline_fatal is False
        assert row.status == "expired"
        assert row.due_date == DUE
