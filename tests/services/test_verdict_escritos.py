"""The verdict also reads the escrito's FILING date (``case_escritos.fecha_ingreso``).

A movement carries the date the court RESOLVED a filing, not the date the lawyer
filed it (median lag 0 days, p90 15, worst 78). Proof case C-11622-2026: the
"Opone excepciones" escrito was filed inside the window, the court provided it 15
days later, and the verdict said ``no_cumplido``.

Escrito evidence is ADDITIVE: it may only ever improve a verdict.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_escrito import CaseEscrito
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine, _EscritosOfCase
from app.services.deadline_verdict import Verdict, evaluate_deadline
from app.services.plazo_copy import build_vencido_text
from app.services.recalcular_veredictos import ejecutar

EXC = "excepciones_8d"
PODER = "acreditar_poder_3d"
TRIGGER = date(2026, 9, 21)
DUE = date(2026, 9, 30)
TODAY = date(2026, 10, 20)


def _escrito(day, tipo="Opone excepciones", solicitante="Demandado"):
    stamp = datetime.combine(day, datetime.min.time()) if day else None
    return SimpleNamespace(fecha_ingreso=stamp, tipo_escrito=tipo, solicitante=solicitante)


def _movement(day, description="Opone excepciones-Traslado", procedure="Resolución", id=10):
    return SimpleNamespace(
        id=id, movement_date=datetime.combine(day, datetime.min.time()),
        description=description, procedure=procedure,
    )


def _evaluate(movements, escritos, due=DUE):
    return evaluate_deadline(
        EXC, due, movements, TODAY, triggered_at=TRIGGER, escritos=escritos
    )


class TestEscritoEvidence:
    def test_c_11622_shape_filed_in_window_court_resolved_later_is_cumplido(self) -> None:
        movements = [_movement(date(2026, 10, 9))]  # court's traslado, 15 days later
        before = _evaluate(movements, None)
        assert before.verdict == Verdict.NO_CUMPLIDO  # today's false alarm

        result = _evaluate(movements, [_escrito(date(2026, 9, 24))])

        assert result.verdict == Verdict.CUMPLIDO
        assert result.acted_on == date(2026, 9, 24)
        assert result.movement_id is None  # evidence is the escrito, not a movement

    def test_filed_on_the_last_day_is_still_in_time(self) -> None:
        assert _evaluate([], [_escrito(DUE)]).verdict == Verdict.CUMPLIDO

    def test_filed_after_due_date_is_fuera_de_plazo_with_no_publication_margin(self) -> None:
        # The filing date is the real date: the publication margin does not apply.
        result = _evaluate([], [_escrito(DUE + timedelta(days=1))])
        assert result.verdict == Verdict.FUERA_DE_PLAZO
        assert result.acted_on == DUE + timedelta(days=1)

    def test_earliest_matching_escrito_wins(self) -> None:
        escritos = [_escrito(DUE + timedelta(days=3)), _escrito(date(2026, 9, 25))]
        assert _evaluate([], escritos).acted_on == date(2026, 9, 25)

    def test_escrito_from_the_other_side_does_not_count(self) -> None:
        other = _escrito(date(2026, 9, 24), solicitante="Demandante")
        assert _evaluate([], [other]) == _evaluate([], None)
        assert _evaluate([], [other]).verdict == Verdict.NO_CUMPLIDO

    @pytest.mark.parametrize("solicitante", ["Ejecutado", "DEMANDADA", "  demandado "])
    def test_debtor_side_spellings_count(self, solicitante) -> None:
        escrito = _escrito(date(2026, 9, 24), solicitante=solicitante)
        assert _evaluate([], [escrito]).verdict == Verdict.CUMPLIDO

    @pytest.mark.parametrize("tipo", ["Téngase presente", "Acompaña documentos", "Contesta traslado"])
    def test_other_kinds_of_escrito_do_not_count(self, tipo) -> None:
        assert _evaluate([], [_escrito(date(2026, 9, 24), tipo=tipo)]).verdict == Verdict.NO_CUMPLIDO

    def test_accent_and_case_tolerant_type_match(self) -> None:
        assert _evaluate([], [_escrito(date(2026, 9, 24), tipo="OPONE  EXCEPCIONES")]).verdict == Verdict.CUMPLIDO

    def test_escrito_without_date_is_ignored(self) -> None:
        assert _evaluate([], [_escrito(None)]).verdict == Verdict.NO_CUMPLIDO

    def test_escrito_filed_before_the_plazo_opened_is_not_evidence(self) -> None:
        early = _escrito(TRIGGER - timedelta(days=5))
        assert _evaluate([], [early]).verdict == Verdict.NO_CUMPLIDO

    def test_other_deadline_types_ignore_escritos(self) -> None:
        result = evaluate_deadline(
            PODER, DUE, [], TODAY, triggered_at=TRIGGER, escritos=[_escrito(date(2026, 9, 24))]
        )
        assert result.verdict == Verdict.NO_CUMPLIDO

    def test_in_force_plazo_with_a_filed_escrito_is_cumplido(self) -> None:
        result = evaluate_deadline(
            EXC, TODAY + timedelta(days=3), [], TODAY, triggered_at=TRIGGER,
            escritos=[_escrito(TODAY)],
        )
        assert result.verdict == Verdict.CUMPLIDO


class TestNeverWorse:
    def test_no_escritos_is_exactly_today(self) -> None:
        movements = [_movement(DUE + timedelta(days=9), "Opone excepciones", "Escrito")]
        assert _evaluate(movements, None) == _evaluate(movements, [])
        assert _evaluate([], None) == _evaluate([], [])

    def test_cumplido_from_movements_is_never_downgraded_by_a_late_escrito(self) -> None:
        movements = [_movement(DUE - timedelta(days=1), "Opone excepciones", "Escrito")]
        late = _escrito(DUE + timedelta(days=20))
        assert _evaluate(movements, None).verdict == Verdict.CUMPLIDO
        result = _evaluate(movements, [late])
        assert result.verdict == Verdict.CUMPLIDO
        assert result.movement_id == 10

    def test_registro_tardio_from_movements_is_not_turned_into_fuera_de_plazo(self) -> None:
        movements = [_movement(DUE + timedelta(days=2), "Opone excepciones", "Escrito")]
        late = _escrito(DUE + timedelta(days=1))
        assert _evaluate(movements, None).verdict == Verdict.REGISTRO_TARDIO
        assert _evaluate(movements, [late]).verdict == Verdict.REGISTRO_TARDIO

    def test_escrito_improves_a_fuera_de_plazo_from_movements(self) -> None:
        movements = [_movement(DUE + timedelta(days=9), "Opone excepciones", "Escrito")]
        assert _evaluate(movements, None).verdict == Verdict.FUERA_DE_PLAZO
        assert _evaluate(movements, [_escrito(date(2026, 9, 24))]).verdict == Verdict.CUMPLIDO


class TestCopy:
    def test_escrito_sourced_fuera_de_plazo_never_says_nothing_was_found(self) -> None:
        _, message = build_vencido_text(
            EXC, DUE, "fuera_de_plazo", "C-1-2026", "Tribunal", filed_on=date(2026, 10, 2)
        )
        assert "No encontramos" not in message
        assert "02-10-2026" in message

    def test_no_cumplido_keeps_the_not_found_sentence(self) -> None:
        _, message = build_vencido_text(EXC, DUE, "no_cumplido", "C-1-2026", "Tribunal")
        assert "No encontramos un escrito presentado dentro del plazo." in message


@pytest.fixture
def owner(db):
    lawyer = Lawyer(rut="55555555-5", email="ve@test.com", name="Escrito Lawyer")
    court = Court(name="Tribunal Civil", code="TCV55", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.flush()
    return lawyer, court


def _case_with_escrito(db, owner, rol, *, side="AB.DDO", solicitante="Demandado"):
    lawyer, court = owner
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", filed_at=datetime(2025, 1, 1), semaforo="verde",
    )
    db.add(case)
    db.flush()
    db.add(CaseLitigante(
        case_id=case.id, participante=side, rut="55555555-5", persona_type="NATURAL",
        nombre="Abogado", natural_key=f"k{case.id}",
    ))
    db.add(Movement(
        case_id=case.id, stage="Excepciones", procedure="Resolución",
        description="Opone excepciones-Traslado",
        movement_date=datetime(2026, 10, 9),
    ))
    db.add(CaseEscrito(
        case_id=case.id, tipo_escrito="Opone excepciones", solicitante=solicitante,
        fecha_ingreso=datetime(2026, 9, 24), natural_key=f"e{case.id}",
    ))
    db.flush()
    return case


class TestEngineAndRecompute:
    def test_engine_verdict_of_an_overdue_row_reads_the_escrito(self, db, owner, monkeypatch) -> None:
        monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
        case = _case_with_escrito(db, owner, "C-11622-2026", side="AB.DTE")
        row = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=TRIGGER,
            status="active",
        )
        db.add(row)
        db.flush()

        movements = db.query(Movement).filter(Movement.case_id == case.id).all()
        DeadlineEngine._record_verdict(row, movements, TODAY, _EscritosOfCase(db, case.id))

        assert row.verdict == "cumplido"
        assert row.verdict_acted_on == date(2026, 9, 24)
        assert row.verdict_movement_id is None

    def test_recalcular_fixes_a_closed_false_no_cumplido(self, db, owner) -> None:
        case = _case_with_escrito(db, owner, "C-11622-2026")
        row = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=TRIGGER,
            status="superseded", verdict="no_cumplido",
            verdict_computed_at=datetime(2026, 10, 1),
        )
        db.add(row)
        db.flush()

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "cumplido"
        assert row.verdict_acted_on == date(2026, 9, 24)
        assert resultado.transiciones == {("no_cumplido", "cumplido"): 1}

    def test_recalcular_ignores_an_escrito_from_the_other_side(self, db, owner) -> None:
        case = _case_with_escrito(db, owner, "C-2-2026", solicitante="Demandante")
        row = CaseDeadline(
            case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=TRIGGER,
            status="superseded", verdict="no_cumplido",
            verdict_computed_at=datetime(2026, 10, 1),
        )
        db.add(row)
        db.flush()

        resultado = ejecutar(db, apply=True, today=TODAY)

        db.refresh(row)
        assert row.verdict == "no_cumplido"
        assert resultado.cambios == []


class TestClosedRowsAreRejudged:
    """A row closed BEFORE its escrito was known must be corrected by a later recompute."""

    @staticmethod
    def _closed(db, owner, rol, **row_kwargs):
        case = _case_with_escrito(db, owner, rol)
        db.query(Movement).filter(Movement.case_id == case.id).delete()  # no classifier trigger
        db.flush()
        defaults = dict(
            case_id=case.id, deadline_type=EXC, due_date=DUE, triggered_at=TRIGGER,
            status="superseded", verdict="no_cumplido",
            verdict_computed_at=datetime(2026, 10, 1),
        )
        defaults.update(row_kwargs)
        row = CaseDeadline(**defaults)
        db.add(row)
        db.flush()
        return case, row

    def test_closed_false_no_cumplido_is_corrected_on_the_next_recompute(
        self, db, owner, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
        case, row = self._closed(db, owner, "C-11622-2026")

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.verdict == "cumplido"
        assert row.verdict_acted_on == date(2026, 9, 24)
        assert row.verdict_movement_id is None
        assert row.status == "superseded"  # lifecycle is not touched

    @pytest.mark.parametrize(
        "kwargs",
        [{"is_manual": True}, {"marked_at": datetime(2026, 10, 2)}, {"status": "no_cumplido"}],
        ids=["is_manual", "marked_at", "auditor_status"],
    )
    def test_auditor_decisions_are_never_overwritten(
        self, db, owner, monkeypatch, kwargs
    ) -> None:
        monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
        case, row = self._closed(db, owner, "C-AUD-1", **kwargs)

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.verdict == "no_cumplido"

    def test_a_better_stored_verdict_is_never_downgraded(self, db, owner, monkeypatch) -> None:
        monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
        case, row = self._closed(db, owner, "C-DOWN-1", verdict="cumplido")
        db.query(CaseEscrito).filter(CaseEscrito.case_id == case.id).update(
            {"fecha_ingreso": datetime(2026, 10, 15)}  # filed late: would be fuera_de_plazo
        )
        db.flush()

        DeadlineEngine.recompute_case(db, case)

        db.refresh(row)
        assert row.verdict == "cumplido"
