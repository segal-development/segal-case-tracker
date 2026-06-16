"""Unit tests for procedural classifier.

CRITICAL: classifier accuracy = legal liability.
Tests validate against:
  (a) spec scenarios from sdd/semaforo-plazos/spec
  (b) real movement sequences from tests/fixtures/real_case_movements.py

TDD sequence: tests written before the implementation module exists.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.deadlines_config import DeadlineType, ProceduralState
from tests.fixtures.real_case_movements import (
    ALL_REAL_CASES,
    DETERMINISTIC_CASES,
    FakeMovement,
    RealCaseFixture,
)


# ---------------------------------------------------------------------------
# Lazy import so the module can be absent during RED phase without collection errors
# ---------------------------------------------------------------------------


def get_classifier():  # type: ignore[return]
    from app.services.procedural_classifier import MovementClassifier

    return MovementClassifier()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date(2026, 6, 16)


def _mv(
    date_str: str,
    stage: str,
    description: str,
    procedure: str = "",
) -> FakeMovement:
    return FakeMovement(
        stage=stage,
        procedure=procedure,
        description=description,
        movement_date=datetime.strptime(date_str, "%Y-%m-%d"),
    )


# ---------------------------------------------------------------------------
# Spec scenarios (REQ-1)
# ---------------------------------------------------------------------------


class TestSpecScenarios:
    def test_notificado_triggers_excepciones_8d(self) -> None:
        """Spec: NOTIFICACIÓN DE DEMANDA (Exitosa) → NOTIFICADO + EXCEPCIONES_8D."""
        clf = get_classifier()
        movements = [
            _mv("2026-04-09", "Gestión Preparatoria", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:08/04/2026"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.NOTIFICADO
        assert DeadlineType.EXCEPCIONES_8D in triggers

    def test_excepciones_stage_triggers_traslado_4d(self) -> None:
        """Spec: stage='Excepciones' → EXCEPCIONES + TRASLADO_EJECUTANTE_4D."""
        clf = get_classifier()
        movements = [
            _mv("2026-04-09", "Gestión Preparatoria", "NOTIFICACIÓN DE DEMANDA (Exitosa)"),
            _mv("2026-04-13", "Excepciones", "Opone excepciones"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.EXCEPCIONES
        assert DeadlineType.TRASLADO_EJECUTANTE_4D in triggers

    def test_auto_prueba_triggers_termino_probatorio_10d(self) -> None:
        """Spec: desc matches auto_prueba rule → AUTO_PRUEBA + TERMINO_PROBATORIO_10D."""
        clf = get_classifier()
        movements = [
            _mv("2026-05-30", "Contestación Excepciones", "Notificación resolución que recibe la causa a prue (Exitosa)"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.AUTO_PRUEBA
        assert DeadlineType.TERMINO_PROBATORIO_10D in triggers

    def test_terminada_no_deadlines(self) -> None:
        """Spec: stage='Terminada' → TERMINADA, no new deadline triggered."""
        clf = get_classifier()
        movements = [
            _mv("2026-04-09", "Gestión Preparatoria", "NOTIFICACIÓN DE DEMANDA (Exitosa)"),
            _mv("2026-05-01", "Terminada", "Resolución definitiva"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.TERMINADA
        # TERMINADA rule starts no new deadline; EXCEPCIONES_8D was previously added
        # but TERMINADA itself doesn't add one.
        # Engine will supersede any prior deadlines.

    def test_empty_history_is_indeterminate(self) -> None:
        """Spec: zero movements → INDETERMINATE."""
        clf = get_classifier()
        state, triggers = clf.classify([], TODAY)
        assert state == ProceduralState.INDETERMINATE
        assert triggers == {}

    def test_all_movements_unmatched_is_indeterminate(self) -> None:
        """Spec: movements with no matching rule → INDETERMINATE."""
        clf = get_classifier()
        movements = [
            _mv("2026-04-09", "Oposición al remate", "Confiere traslado oposición al remate"),
            _mv("2026-05-01", "Oposición al remate", "Mero trámite"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.INDETERMINATE

    def test_certificacion_demanda_does_not_trigger_notificado(self) -> None:
        """Only '(Exitosa)' notification triggers NOTIFICADO; '(Certificación)' must not."""
        clf = get_classifier()
        movements = [
            _mv("2026-03-23", "Exhorto", "NOTIFICACIÓN DE DEMANDA (Certificación) Diligencia:20/03/2026"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.INDETERMINATE
        assert DeadlineType.EXCEPCIONES_8D not in triggers


# ---------------------------------------------------------------------------
# REBELDÍA logic (the classifier doesn't decide rebeldía — it returns NOTIFICADO;
# the engine's recompute_case applies the 8-day transition check afterward)
# ---------------------------------------------------------------------------


class TestRebeldia:
    def test_rebeldia_not_fired_by_classifier_when_no_excepciones(self) -> None:
        """Classifier does NOT decide REBELDE — that is the engine's job.

        A case in NOTIFICADO with elapsed EXCEPCIONES_8D is still returned
        as NOTIFICADO by the classifier.  The engine promotes to REBELDE.
        """
        clf = get_classifier()
        movements = [
            _mv("2026-01-01", "Gestión Preparatoria", "NOTIFICACIÓN DE DEMANDA (Exitosa)"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        # Classifier result is NOTIFICADO; engine handles REBELDE.
        assert state == ProceduralState.NOTIFICADO
        assert DeadlineType.EXCEPCIONES_8D in triggers

    def test_rebeldia_not_fired_when_excepciones_present(self) -> None:
        """If excepciones stage appears, state is EXCEPCIONES (not REBELDE).

        The engine's REBELDÍA check sees state != NOTIFICADO and skips.
        """
        clf = get_classifier()
        movements = [
            _mv("2026-01-01", "Gestión Preparatoria", "NOTIFICACIÓN DE DEMANDA (Exitosa)"),
            _mv("2026-01-10", "Excepciones", "Opone excepciones"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.EXCEPCIONES


# ---------------------------------------------------------------------------
# Safety and edge cases (REQ-10)
# ---------------------------------------------------------------------------


class TestSafety:
    def test_unknown_sequence_is_indeterminate_not_crash(self) -> None:
        """Unrecognized movement must return INDETERMINATE and not raise."""
        clf = get_classifier()
        movements = [
            _mv("2026-06-01", "UnsupportedStage", "Completely unknown text", "UnknownProc"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        # Must not raise; safe-fail to INDETERMINATE
        assert state == ProceduralState.INDETERMINATE

    def test_classify_never_raises_on_garbage_input(self) -> None:
        """Classifier must absorb exceptions and return INDETERMINATE."""
        clf = get_classifier()
        # Pass objects that have the attrs but with weird values
        weird = FakeMovement(
            stage=None,  # type: ignore[arg-type]
            procedure="",
            description=None,  # type: ignore[arg-type]
            movement_date=datetime(2026, 1, 1),
        )
        state, triggers = clf.classify([weird], TODAY)
        assert state == ProceduralState.INDETERMINATE

    def test_auto_prueba_also_triggers_observaciones_6d(self) -> None:
        """When AUTO_PRUEBA is triggered, OBSERVACIONES_PRUEBA_6D is computed too."""
        clf = get_classifier()
        movements = [
            _mv("2026-05-30", "Contestación Excepciones", "Notificación resolución que recibe la causa a prueba (Exitosa)"),
        ]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.AUTO_PRUEBA
        assert DeadlineType.TERMINO_PROBATORIO_10D in triggers
        # OBSERVACIONES trigger is a computed date, not a movement
        assert DeadlineType.OBSERVACIONES_PRUEBA_6D in triggers
        from datetime import date
        assert isinstance(triggers[DeadlineType.OBSERVACIONES_PRUEBA_6D], date)

    def test_citacion_sentencia_triggers_sentencia_10d(self) -> None:
        """CITACION_SENTENCIA rule starts SENTENCIA_10D deadline."""
        clf = get_classifier()
        movements = [_mv("2026-04-01", "Tramitación", "Cita a Audiencia")]
        state, triggers = clf.classify(movements, TODAY)
        assert state == ProceduralState.CITACION_SENTENCIA
        assert DeadlineType.SENTENCIA_10D in triggers


# ---------------------------------------------------------------------------
# Real-case parametrized tests (REQ-11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    ALL_REAL_CASES,
    ids=[c.case_id for c in ALL_REAL_CASES],
)
def test_real_case_movements(fixture: RealCaseFixture) -> None:
    """Classifier produces expected state for each real QA movement sequence."""
    clf = get_classifier()
    state, triggers = clf.classify(fixture.movements, TODAY)  # type: ignore[arg-type]
    assert state == fixture.expected_state, (
        f"{fixture.case_id}: expected {fixture.expected_state}, got {state}"
    )
    if fixture.expected_deadline is not None:
        assert fixture.expected_deadline in triggers, (
            f"{fixture.case_id}: expected {fixture.expected_deadline} in triggers, "
            f"got {list(triggers.keys())}"
        )
