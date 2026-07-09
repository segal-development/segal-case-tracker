"""Unit tests for DecisionEngine — DEFENSE-side action recommendation (req #5/#11).

Pure rules engine tests: no DB, no ORM ``Case`` — mirrors the
``TestComputePrescripcion`` pattern in ``tests/unit/test_prescripcion.py``
(``types.SimpleNamespace`` stubs carrying only the attributes the engine
reads).  Wiring into ``DeadlineEngine.recompute_case`` is covered separately
in ``tests/unit/test_deadline_engine.py``.

Legal source: memory/juicio-ejecutivo-defense-rules.md (confirmed by the
firm 2026-07-09). The firm's side is always DEMANDADO/EJECUTADO.
"""

from __future__ import annotations

import types
from datetime import date, timedelta

from app.core.deadlines_config import ProceduralState
from app.core.decision_rules import Urgency
from app.services.decision_engine import DecisionEngine, Recommendation

TODAY = date(2026, 7, 9)


def _case(
    *,
    procedural_state: str | None = None,
    semaforo: str | None = None,
    abandono_disponible: bool = False,
    prescripcion_cumplida: bool = False,
    en_apremio: bool = False,
    next_deadline_at: date | None = None,
    next_deadline_fatal: bool = False,
    case_id: int = 1,
):
    """Minimal stub carrying only the DeadlineEngine-computed attributes
    DecisionEngine.recommend/compute_next_review_at read."""
    return types.SimpleNamespace(
        id=case_id,
        procedural_state=procedural_state,
        semaforo=semaforo,
        abandono_disponible=abandono_disponible,
        prescripcion_cumplida=prescripcion_cumplida,
        en_apremio=en_apremio,
        next_deadline_at=next_deadline_at,
        next_deadline_fatal=next_deadline_fatal,
    )


# ---------------------------------------------------------------------------
# Each rule fires for its situation
# ---------------------------------------------------------------------------


class TestRecommendIndividualRules:
    def test_open_excepciones_window_recommends_oponer_excepciones(self) -> None:
        case = _case(
            procedural_state=ProceduralState.NOTIFICADO.value,
            semaforo="amarillo",
            next_deadline_at=TODAY + timedelta(days=3),
            next_deadline_fatal=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "oponer_excepciones"
        assert rec.urgency == Urgency.CRITICA
        assert "459" in rec.legal_basis or "464" in rec.legal_basis

    def test_prescripcion_cumplida_recommends_oponer_prescripcion(self) -> None:
        case = _case(prescripcion_cumplida=True, semaforo="verde")
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "oponer_prescripcion"

    def test_abandono_disponible_recommends_solicitar_abandono(self) -> None:
        case = _case(abandono_disponible=True, semaforo="verde")
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "solicitar_abandono"

    def test_auto_prueba_state_recommends_reposicion_prueba(self) -> None:
        case = _case(procedural_state=ProceduralState.AUTO_PRUEBA.value, semaforo="amarillo")
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "reposicion_prueba"

    def test_sentencia_with_open_apelacion_window_recommends_evaluar_apelacion(self) -> None:
        case = _case(
            procedural_state=ProceduralState.SENTENCIA.value,
            semaforo="amarillo",
            next_deadline_at=TODAY + timedelta(days=2),
            next_deadline_fatal=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "evaluar_apelacion"

    def test_en_apremio_recommends_gestion_apremio(self) -> None:
        case = _case(en_apremio=True, semaforo="rojo")
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "gestion_apremio"

    def test_semaforo_rojo_fatal_with_no_specific_rule_recommends_atencion_inmediata(self) -> None:
        """ROJO + fatal deadline, but procedural_state doesn't match any
        specific rule (e.g. TRASLADO_EJECUTANTE) → generic catch-all."""
        case = _case(
            procedural_state=ProceduralState.TRASLADO_EJECUTANTE.value,
            semaforo="rojo",
            next_deadline_at=TODAY - timedelta(days=1),
            next_deadline_fatal=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "atencion_inmediata"

    def test_nothing_pending_returns_none(self) -> None:
        case = _case(procedural_state=ProceduralState.MANDAMIENTO.value, semaforo="verde")
        assert DecisionEngine.recommend(case, today=TODAY) is None

    def test_expired_excepciones_window_does_not_recommend_oponer_excepciones(self) -> None:
        """Window already missed (due_date < today) → not 'oponer_excepciones'
        (the opportunity to oppose within-window is gone); falls through to
        the generic rojo+fatal catch-all instead."""
        case = _case(
            procedural_state=ProceduralState.NOTIFICADO.value,
            semaforo="rojo",
            next_deadline_at=TODAY - timedelta(days=1),
            next_deadline_fatal=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "atencion_inmediata"


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestRecommendPriority:
    def test_active_fatal_excepciones_beats_abandono_disponible(self) -> None:
        case = _case(
            procedural_state=ProceduralState.NOTIFICADO.value,
            semaforo="amarillo",
            next_deadline_at=TODAY + timedelta(days=3),
            next_deadline_fatal=True,
            abandono_disponible=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "oponer_excepciones"

    def test_prescripcion_beats_generic_rojo(self) -> None:
        case = _case(
            procedural_state=ProceduralState.TRASLADO_EJECUTANTE.value,
            semaforo="rojo",
            next_deadline_at=TODAY - timedelta(days=1),
            next_deadline_fatal=True,
            prescripcion_cumplida=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "oponer_prescripcion"

    def test_abandono_beats_generic_rojo(self) -> None:
        case = _case(
            procedural_state=ProceduralState.TRASLADO_EJECUTANTE.value,
            semaforo="rojo",
            next_deadline_at=TODAY - timedelta(days=1),
            next_deadline_fatal=True,
            abandono_disponible=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "solicitar_abandono"

    def test_prescripcion_beats_abandono(self) -> None:
        case = _case(prescripcion_cumplida=True, abandono_disponible=True, semaforo="verde")
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "oponer_prescripcion"

    def test_standing_opportunity_beats_auto_prueba(self) -> None:
        case = _case(
            procedural_state=ProceduralState.AUTO_PRUEBA.value,
            semaforo="amarillo",
            abandono_disponible=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "solicitar_abandono"

    def test_auto_prueba_beats_apremio(self) -> None:
        case = _case(
            procedural_state=ProceduralState.AUTO_PRUEBA.value,
            semaforo="amarillo",
            en_apremio=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "reposicion_prueba"

    def test_apremio_beats_generic_rojo(self) -> None:
        case = _case(
            procedural_state=ProceduralState.TRASLADO_EJECUTANTE.value,
            semaforo="rojo",
            next_deadline_at=TODAY - timedelta(days=1),
            next_deadline_fatal=True,
            en_apremio=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "gestion_apremio"


# ---------------------------------------------------------------------------
# Safe-fail — NEVER raises
# ---------------------------------------------------------------------------


class TestRecommendNeverRaises:
    def test_empty_stub_returns_none(self) -> None:
        """Completely bare object (no expected attributes at all) → None, no raise."""
        assert DecisionEngine.recommend(types.SimpleNamespace(), today=TODAY) is None

    def test_none_case_returns_none(self) -> None:
        assert DecisionEngine.recommend(None, today=TODAY) is None

    def test_malformed_next_deadline_at_type_returns_none_not_raise(self) -> None:
        """next_deadline_at is a str instead of a date → comparison would
        raise TypeError; recommend() must swallow it and return None."""
        case = _case(
            procedural_state=ProceduralState.NOTIFICADO.value,
            next_deadline_at="not-a-date",  # type: ignore[arg-type]
            next_deadline_fatal=True,
        )
        assert DecisionEngine.recommend(case, today=TODAY) is None

    def test_default_clock_used_when_today_omitted(self, monkeypatch) -> None:
        monkeypatch.setattr("app.services.decision_engine._today_chile", lambda: TODAY)
        case = _case(prescripcion_cumplida=True)
        rec = DecisionEngine.recommend(case)
        assert rec is not None
        assert rec.code == "oponer_prescripcion"


# ---------------------------------------------------------------------------
# compute_next_review_at
# ---------------------------------------------------------------------------


class TestComputeNextReviewAt:
    def test_returns_next_deadline_at_when_present(self) -> None:
        due = TODAY + timedelta(days=5)
        case = _case(next_deadline_at=due, next_deadline_fatal=True)
        assert DecisionEngine.compute_next_review_at(case, today=TODAY) == due

    def test_returns_short_horizon_when_abandono_pending_and_no_deadline(self) -> None:
        case = _case(abandono_disponible=True)
        result = DecisionEngine.compute_next_review_at(case, today=TODAY)
        assert result is not None
        assert result > TODAY

    def test_returns_short_horizon_when_prescripcion_cumplida_and_no_deadline(self) -> None:
        case = _case(prescripcion_cumplida=True)
        result = DecisionEngine.compute_next_review_at(case, today=TODAY)
        assert result is not None
        assert result > TODAY

    def test_returns_short_horizon_when_en_apremio_and_no_deadline(self) -> None:
        case = _case(en_apremio=True)
        result = DecisionEngine.compute_next_review_at(case, today=TODAY)
        assert result is not None
        assert result > TODAY

    def test_standing_opportunity_review_date_is_staggered_by_case_id(self) -> None:
        """A standing-opportunity case's review date must be
        today + 7 + (case.id % 30) — this spreads reviews over a
        30-day window instead of piling every no-deadline opportunity
        onto the exact same calendar day."""
        case = _case(abandono_disponible=True, case_id=41)
        expected = TODAY + timedelta(days=7 + (41 % 30))
        assert DecisionEngine.compute_next_review_at(case, today=TODAY) == expected

    def test_standing_opportunity_review_date_differs_across_case_ids(self) -> None:
        """Two cases whose ids differ mod 30 must land on DIFFERENT
        review dates — proves the staggering actually spreads load."""
        case_a = _case(abandono_disponible=True, case_id=1)
        case_b = _case(abandono_disponible=True, case_id=2)
        result_a = DecisionEngine.compute_next_review_at(case_a, today=TODAY)
        result_b = DecisionEngine.compute_next_review_at(case_b, today=TODAY)
        assert result_a != result_b

    def test_standing_opportunity_review_date_is_stable_for_same_case_id(self) -> None:
        """Recomputing for the same case.id must yield the SAME date —
        no randomness, deterministic staggering."""
        case = _case(prescripcion_cumplida=True, case_id=17)
        first = DecisionEngine.compute_next_review_at(case, today=TODAY)
        second = DecisionEngine.compute_next_review_at(case, today=TODAY)
        assert first == second

    def test_standing_opportunity_review_date_falls_back_when_case_id_is_none(self) -> None:
        """A case with a missing/None id must fall back to the base
        offset (today + 7) without raising."""
        case = _case(en_apremio=True, case_id=None)
        result = DecisionEngine.compute_next_review_at(case, today=TODAY)
        assert result == TODAY + timedelta(days=7)

    def test_returns_none_when_nothing_pending(self) -> None:
        case = _case(procedural_state=ProceduralState.MANDAMIENTO.value)
        assert DecisionEngine.compute_next_review_at(case, today=TODAY) is None

    def test_next_deadline_at_takes_priority_over_standing_opportunities(self) -> None:
        due = TODAY + timedelta(days=3)
        case = _case(next_deadline_at=due, next_deadline_fatal=True, abandono_disponible=True)
        assert DecisionEngine.compute_next_review_at(case, today=TODAY) == due

    def test_never_raises_on_malformed_case(self) -> None:
        case = _case(next_deadline_at="not-a-date")  # type: ignore[arg-type]
        # Malformed input must not raise — either a safe fallback date or None.
        DecisionEngine.compute_next_review_at(case, today=TODAY)

    def test_none_case_returns_none(self) -> None:
        assert DecisionEngine.compute_next_review_at(None, today=TODAY) is None


# ---------------------------------------------------------------------------
# Recommendation dataclass shape
# ---------------------------------------------------------------------------


class TestRecommendationShape:
    def test_recommendation_carries_related_deadline_at(self) -> None:
        due = TODAY + timedelta(days=3)
        case = _case(
            procedural_state=ProceduralState.NOTIFICADO.value,
            next_deadline_at=due,
            next_deadline_fatal=True,
        )
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert isinstance(rec, Recommendation)
        assert rec.related_deadline_at == due
