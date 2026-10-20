"""An overdue plazo must be CLOSED, not left ``active`` forever.

Measured on QA: 1,705 of 1,758 ``active`` rows were already past their due date,
because step 6b of the engine recorded a verdict but never touched ``status``.
Those rows kept feeding ``next_deadline_at`` (a date months in the past) and the
semaforo. The closure writes ``expired`` and must:

* leave a plazo still in force ``active``;
* wait out the publication margin for types that have a verdict detector (an act
  filed on time can be published late; PR #379 fixed that false miss);
* never touch manual or auditor-marked rows;
* keep the closed row re-judgeable when an escrito shows up later (PR #379).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.core.deadlines_config import DeadlineType
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.deadline_engine import DeadlineEngine
from app.services.deadline_verdict import default_margin_days

EXC = DeadlineType.EXCEPCIONES_8D.value
TRASLADO = DeadlineType.TRASLADO_EJECUTANTE_4D.value
PODER = DeadlineType.ACREDITAR_PODER_3D.value

NOTIF = date(2026, 6, 1)  # Monday
EXC_DUE = add_business_days(NOTIF, 8)
EXC_MARGIN = default_margin_days(EXC)

TRASLADO_AT = date(2026, 6, 1)
TRASLADO_DUE = add_business_days(TRASLADO_AT, 4)


def _freeze(monkeypatch, today: date) -> None:
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: today)


@pytest.fixture
def owner(db):
    lawyer = Lawyer(rut="77777777-7", email="cp@test.com", name="Cerrar Plazos")
    court = Court(name="Tribunal Civil", code="TCP77", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _case(db, owner, rol, side) -> Case:
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
    return case


def _movement(db, case, day: date, stage: str, description: str) -> None:
    db.add(Movement(
        case_id=case.id, stage=stage, procedure="Resolución", description=description,
        movement_date=datetime.combine(day, datetime.min.time()),
    ))
    db.flush()


def _traslado_case(db, owner, rol="C-TR-1") -> Case:
    """DEMANDANTE with a live traslado_ejecutante_4d (an actionable, mandatory plazo)."""
    case = _case(db, owner, rol, "AB.DTE")
    _movement(db, case, TRASLADO_AT - timedelta(days=10), "Gestión",
              "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _movement(db, case, TRASLADO_AT, "Contestación Excepciones",
              "Confiere traslado al ejecutante")
    return case


def _excepciones_case(db, owner, rol="C-EX-1") -> Case:
    """DEMANDADO whose excepciones_8d runs from NOTIF."""
    case = _case(db, owner, rol, "AB.DDO")
    _movement(db, case, NOTIF, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    return case


def _row(db, case, deadline_type) -> CaseDeadline:
    return (
        db.query(CaseDeadline)
        .filter_by(case_id=case.id, deadline_type=deadline_type)
        .order_by(CaseDeadline.id.desc())
        .first()
    )


class TestOverdueRowCloses:
    def test_overdue_row_with_no_act_is_closed(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, TRASLADO_DUE + timedelta(days=30))
        case = _traslado_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        assert _row(db, case, TRASLADO).status == "expired"

    def test_next_deadline_at_never_returns_a_past_date(self, db, owner, monkeypatch) -> None:
        today = TRASLADO_DUE + timedelta(days=30)
        _freeze(monkeypatch, today)
        case = _traslado_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        assert case.next_deadline_at is None
        assert case.next_deadline_fatal is False

    def test_a_missed_mandatory_plazo_still_makes_the_case_rojo(
        self, db, owner, monkeypatch
    ) -> None:
        # Closing the row must not silently turn a really missed obligation green:
        # "expired + mandatory" is exactly what the semaforo reads as ROJO.
        _freeze(monkeypatch, TRASLADO_DUE + timedelta(days=30))
        case = _traslado_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        assert case.semaforo == "rojo"

    def test_closing_is_idempotent(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, TRASLADO_DUE + timedelta(days=30))
        case = _traslado_case(db, owner)

        DeadlineEngine.recompute_case(db, case)
        DeadlineEngine.recompute_case(db, case)

        rows = db.query(CaseDeadline).filter_by(case_id=case.id, deadline_type=TRASLADO).all()
        assert [r.status for r in rows] == ["expired"]


class TestPlazoInForceStaysActive:
    def test_due_today_is_still_active(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, TRASLADO_DUE)
        case = _traslado_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        assert _row(db, case, TRASLADO).status == "active"
        assert case.next_deadline_at == TRASLADO_DUE

    def test_future_row_is_never_a_candidate(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, TRASLADO_DUE - timedelta(days=2))
        case = _traslado_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        assert _row(db, case, TRASLADO).status == "active"

    def test_effective_due_date_wins_over_a_stale_column(self, db, owner) -> None:
        # due_date column says "long past", but the plazo SHOULD end tomorrow.
        today = date(2026, 6, 16)
        case = _case(db, owner, "C-STALE-1", "AB.DTE")
        source = Movement(
            case_id=case.id, stage="x", procedure="Resolución", description="x",
            movement_date=datetime.combine(today - timedelta(days=1), datetime.min.time()),
        )
        db.add(source)
        db.flush()
        row = CaseDeadline(
            case_id=case.id, deadline_type=TRASLADO, due_date=today - timedelta(days=30),
            triggered_at=today - timedelta(days=1), status="active",
            source_movement_id=source.id,
        )
        db.add(row)
        db.flush()

        assert DeadlineEngine._current_due_date(row) >= today
        assert DeadlineEngine._term_elapsed(row, today) is False


class TestPublicationMargin:
    @pytest.mark.parametrize("late_by", range(1, EXC_MARGIN + 1))
    def test_inside_the_margin_the_row_stays_active(
        self, db, owner, monkeypatch, late_by
    ) -> None:
        _freeze(monkeypatch, EXC_DUE + timedelta(days=late_by))
        case = _excepciones_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        assert _row(db, case, EXC).status == "active"

    def test_one_day_past_the_margin_it_closes(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, EXC_DUE + timedelta(days=EXC_MARGIN + 1))
        case = _excepciones_case(db, owner)

        DeadlineEngine.recompute_case(db, case)

        row = _row(db, case, EXC)
        assert row.status == "expired"
        assert row.verdict == "no_cumplido"

    def test_poder_waits_its_longer_margin_then_closes(self, db, owner, monkeypatch) -> None:
        previo = date(2026, 6, 1)
        due = add_business_days(previo, 3)
        margin = default_margin_days(PODER)
        case = _case(db, owner, "C-PODER-M", "AB.DDO")
        _movement(db, case, previo, "Inicio de la Tramitación", "Previo a proveer")

        _freeze(monkeypatch, due + timedelta(days=margin))
        DeadlineEngine.recompute_case(db, case)
        assert _row(db, case, PODER).status == "active"

        _freeze(monkeypatch, due + timedelta(days=margin + 1))
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case, PODER)
        assert row.status == "expired"
        assert row.verdict == "no_cumplido"

    def test_margin_is_part_of_the_term_for_poder(self) -> None:
        row = CaseDeadline(
            case_id=1, deadline_type=PODER, due_date=date(2026, 6, 4),
            triggered_at=date(2026, 6, 1), status="active",
        )
        margin = default_margin_days(PODER)
        assert margin > EXC_MARGIN
        assert DeadlineEngine._term_elapsed(row, row.due_date + timedelta(days=margin)) is False
        assert DeadlineEngine._term_elapsed(row, row.due_date + timedelta(days=margin + 1)) is True


class TestNeverOverwriteHumanState:
    def test_manual_row_is_untouched(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, date(2026, 10, 20))
        case = _case(db, owner, "C-MAN-1", "AB.DTE")
        _movement(db, case, date(2026, 5, 1), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        row = CaseDeadline(
            case_id=case.id, deadline_type=TRASLADO, due_date=date(2026, 6, 1),
            triggered_at=date(2026, 5, 27), status="active", is_manual=True,
        )
        db.add(row)
        db.flush()

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.status == "active"

    def test_row_marked_by_an_auditor_is_untouched(self, db, owner, monkeypatch) -> None:
        _freeze(monkeypatch, TRASLADO_DUE + timedelta(days=30))
        case = _traslado_case(db, owner)
        lawyer, _ = owner
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case, TRASLADO)
        # The auditor reopened it by hand.
        row.status = "active"
        row.marked_by = lawyer.id
        row.marked_at = datetime(2026, 10, 1)
        db.flush()

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.status == "active"

    @pytest.mark.parametrize("status", ["cumplido", "no_cumplido", "superseded", "expired"])
    def test_terminal_rows_are_not_candidates(self, db, owner, monkeypatch, status) -> None:
        _freeze(monkeypatch, date(2026, 10, 20))
        case = _case(db, owner, f"C-T-{status}", "AB.DTE")
        _movement(db, case, date(2026, 5, 1), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        row = CaseDeadline(
            case_id=case.id, deadline_type=TRASLADO, due_date=date(2026, 6, 1),
            triggered_at=date(2026, 5, 27), status=status, is_manual=True,
        )
        db.add(row)
        db.flush()

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.status == status


class TestClosedRowRemainsRejudgeable:
    def test_a_later_escrito_still_corrects_a_row_closed_by_this_step(
        self, db, owner, monkeypatch
    ) -> None:
        # The PR #379 path: closed with a movement-only miss, escrito arrives later.
        today = EXC_DUE + timedelta(days=EXC_MARGIN + 10)
        _freeze(monkeypatch, today)
        case = _case(db, owner, "C-REJ-1", "AB.DDO")
        trigger = Movement(
            case_id=case.id, stage="Gestión", procedure="Resolución",
            description="NOTIFICACIÓN DE DEMANDA (Exitosa)",
            movement_date=datetime.combine(NOTIF, datetime.min.time()),
        )
        db.add(trigger)
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        row = _row(db, case, EXC)
        assert (row.status, row.verdict) == ("expired", "no_cumplido")

        # The case moves on (no classifier trigger any more) and the escrito is scraped.
        db.query(Movement).filter(Movement.case_id == case.id).delete()
        db.add(CaseEscrito(
            case_id=case.id, tipo_escrito="Opone excepciones", solicitante="Demandado",
            fecha_ingreso=datetime.combine(EXC_DUE - timedelta(days=2), datetime.min.time()),
            natural_key=f"e{case.id}",
        ))
        db.flush()

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.verdict == "cumplido"
        # The case advanced, so the closed row is superseded (as a live one would
        # be); the re-judgement only writes the verdict columns.
        assert row.status == "superseded"


class TestClosedRowIsStillSupersededWhenTheCaseAdvances:
    """"expired" + mandatory reads as ROJO, so a closed row must not outlive its state."""

    def test_the_case_advancing_supersedes_a_row_closed_earlier(
        self, db, owner, monkeypatch
    ) -> None:
        _freeze(monkeypatch, TRASLADO_DUE + timedelta(days=30))
        case = _traslado_case(db, owner)
        DeadlineEngine.recompute_case(db, case)
        assert _row(db, case, TRASLADO).status == "expired"
        assert case.semaforo == "rojo"

        _movement(db, case, TRASLADO_DUE + timedelta(days=10), "Tramitación", "Citación a oír sentencia")
        DeadlineEngine.recompute_case(db, case)

        assert _row(db, case, TRASLADO).status == "superseded"
        assert case.semaforo != "rojo"
