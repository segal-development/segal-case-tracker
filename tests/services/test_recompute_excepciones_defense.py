"""Forced recompute of cases whose excepciones_8d plazo a filed defense closes."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.business_days import add_business_days
from app.services.recompute_excepciones_defense import run

TODAY = date(2026, 6, 16)
NOTIF_DAY = TODAY - timedelta(days=30)
DUE = add_business_days(NOTIF_DAY, 8)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def owner(db):
    lawyer = Lawyer(rut="44444444-4", email="rc@test.com", name="Recompute Lawyer")
    court = Court(name="Tribunal Civil", code="TCV77", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _case(db, owner, rol, side="demandado", escrito=None, escrito_day=DUE) -> Case:
    lawyer, court = owner
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", filed_at=datetime(2025, 1, 1),
    )
    db.add(case)
    db.flush()
    code = "AB.DTE" if side == "demandante" else "AB.DDO"
    db.add(CaseLitigante(
        case_id=case.id, participante=code, rut="44444444-4",
        persona_type="NATURAL", nombre="Abogado", natural_key=f"k{case.id}",
    ))
    db.add(Movement(
        case_id=case.id, stage="Gestión", procedure="Resolución",
        description="NOTIFICACIÓN DE DEMANDA (Exitosa)",
        movement_date=datetime.combine(NOTIF_DAY, datetime.min.time()),
    ))
    if escrito:
        db.add(CaseEscrito(
            case_id=case.id, tipo_escrito=escrito, solicitante="Demandado",
            fecha_ingreso=datetime.combine(escrito_day, datetime.min.time()),
            natural_key=f"e{case.id}",
        ))
    db.flush()
    return case


def _stale_active_row(db, case) -> CaseDeadline:
    """The production state: an active, overdue row and a red semaforo."""
    row = CaseDeadline(
        case_id=case.id, deadline_type="excepciones_8d", due_date=DUE,
        triggered_at=NOTIF_DAY, status="active",
    )
    db.add(row)
    case.semaforo = "rojo"
    db.flush()
    return row


def test_dry_run_reports_outcome_but_persists_nothing(db, owner):
    case = _case(db, owner, "C-1-2026", escrito="Opone excepciones")
    row = _stale_active_row(db, case)
    db.commit()  # production data is committed; the dry-run rollback must not eat it
    case_id, row_id = case.id, row.id

    result = run(db)  # dry-run is the default

    assert result.candidates == 1
    assert result.to_cumplido == 1
    assert result.rojo_cleared == 1
    db.expire_all()
    assert db.get(CaseDeadline, row_id).status == "active"
    assert db.get(Case, case_id).semaforo == "rojo"


def test_apply_closes_row_and_clears_rojo(db, owner):
    on_time = _case(db, owner, "C-2-2026", escrito="Opone excepciones")
    late = _case(
        db, owner, "C-3-2026", escrito="Opone excepciones",
        escrito_day=DUE + timedelta(days=4),
    )
    r_on_time, r_late = _stale_active_row(db, on_time), _stale_active_row(db, late)

    result = run(db, apply=True)

    assert (result.to_cumplido, result.to_no_cumplido, result.unchanged) == (1, 1, 0)
    db.expire_all()
    assert db.get(CaseDeadline, r_on_time.id).status == "cumplido"
    assert db.get(CaseDeadline, r_late.id).status == "no_cumplido"
    assert db.get(Case, on_time.id).semaforo != "rojo"
    # Audit Fase 0 (S3): a fatal deadline closed as no_cumplido keeps the case rojo
    # until a lawyer or admin reviews and marks it.
    assert db.get(Case, late.id).semaforo == "rojo"


def test_second_run_finds_nothing(db, owner):
    case = _case(db, owner, "C-4-2026", escrito="Opone excepciones")
    _stale_active_row(db, case)
    run(db, apply=True)

    again = run(db, apply=True)

    assert again.candidates == 0
    assert again.outcomes == []


def test_case_without_defense_is_not_a_candidate(db, owner):
    case = _case(db, owner, "C-5-2026", escrito="Tenga presente")
    row = _stale_active_row(db, case)

    result = run(db, apply=True)

    assert result.candidates == 0
    db.expire_all()
    assert db.get(CaseDeadline, row.id).status == "active"
    assert db.get(Case, case.id).semaforo == "rojo"


def test_demandante_case_is_skipped(db, owner):
    case = _case(db, owner, "C-6-2026", side="demandante", escrito="Opone excepciones")
    row = _stale_active_row(db, case)

    result = run(db, apply=True)

    assert result.skipped_not_demandado == 1
    assert result.outcomes == []
    db.expire_all()
    assert db.get(CaseDeadline, row.id).status == "active"


def test_manual_row_is_not_a_candidate(db, owner):
    case = _case(db, owner, "C-7-2026", escrito="Opone excepciones")
    row = _stale_active_row(db, case)
    row.is_manual = True
    db.flush()

    assert run(db, apply=True).candidates == 0


def test_reanchored_row_reports_the_new_rows_verdict_not_superseded(db, owner):
    """Population shape: the stored row has an OLD anchor, so the engine
    supersedes it and judges a NEW row. The report must not count that as a
    plain 'superseded' (it hid 20 of 41 closures in the QA dry-run)."""
    case = _case(db, owner, "C-8-2026", escrito="Opone excepciones")
    row = CaseDeadline(
        case_id=case.id, deadline_type="excepciones_8d",
        due_date=DUE - timedelta(days=4), triggered_at=NOTIF_DAY - timedelta(days=4),
        status="active",
    )
    db.add(row)
    case.semaforo = "rojo"
    db.flush()

    result = run(db, apply=True)

    assert (result.to_cumplido, result.to_no_cumplido, result.other) == (1, 0, 0)
    assert "superseded" in result.outcomes[0].rows_after
    assert "cumplido" in result.outcomes[0].rows_after


def test_state_advanced_case_with_on_time_defense_is_cumplido_not_superseded(db, owner):
    case = _case(db, owner, "C-9-2026", escrito="Opone excepciones")
    db.add(Movement(
        case_id=case.id, stage="Gestión", procedure="Resolución",
        description="Se pronuncia sobre admisibilidad",
        movement_date=datetime.combine(NOTIF_DAY + timedelta(days=3), datetime.min.time()),
    ))
    _stale_active_row(db, case)

    result = run(db, apply=True)

    assert (result.to_cumplido, result.other) == (1, 0)


def test_late_defense_writes_and_reports_plazo_fatal_incumplido(db, owner):
    on_time = _case(db, owner, "C-20-2026", escrito="Opone excepciones", escrito_day=DUE)
    late = _case(
        db, owner, "C-21-2026", escrito="Opone excepciones",
        escrito_day=DUE + timedelta(days=3),
    )
    _stale_active_row(db, on_time)
    _stale_active_row(db, late)

    result = run(db, apply=True)

    assert result.to_fatal_flagged == 1
    db.expire_all()
    assert db.get(Case, late.id).plazo_fatal_incumplido is True
    assert db.get(Case, on_time.id).plazo_fatal_incumplido is False


def test_dry_run_reports_the_flag_but_persists_nothing(db, owner):
    late = _case(
        db, owner, "C-22-2026", escrito="Opone excepciones",
        escrito_day=DUE + timedelta(days=3),
    )
    _stale_active_row(db, late)
    db.commit()
    case_id = late.id

    result = run(db)

    assert result.to_fatal_flagged == 1
    db.expire_all()
    assert db.get(Case, case_id).plazo_fatal_incumplido is False
