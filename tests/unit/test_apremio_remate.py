"""Unit tests for the REMATE apremio sub-stage refinement (recommendation only).

Two pieces are covered:

  1. ``DeadlineEngine._compute_apremio_substage`` — detects when an apremio
     case has actually reached the *remate* (auction) sub-stage, mirroring
     ``_compute_en_apremio``'s movement scan and never-raises contract.

  2. The ``gestion_remate`` DecisionRule — a MORE SPECIFIC recommendation that
     supersedes the generic ``gestion_apremio`` when the transient
     ``case._apremio_substage == "remate"``.

GUARDRAIL: this refinement carries NO deadline. It only makes the existing
apremio recommendation more specific; it must not add or change any
next_deadline_at / CaseDeadline logic. These tests assert that.

Legal source: memory/juicio-ejecutivo-defense-rules.md (arts. 486-518 CPC).
"""

from __future__ import annotations

import types
from datetime import date, timedelta

from app.core.decision_rules import (
    DecisionRule,
    RECOMMENDATION_DISCLAIMER,
    Urgency,
    resolve_rule,
)
from app.services.deadline_engine import DeadlineEngine
from app.services.decision_engine import DecisionEngine

TODAY = date(2026, 7, 10)


def _mv(*, stage: str | None = None, description: str = ""):
    """Minimal movement stub carrying only the fields the scan reads."""
    return types.SimpleNamespace(stage=stage, description=description)


def _case_with_movements(movements):
    """Case stub whose ``__dict__['movements']`` holds the loaded relationship,
    exactly the fast path ``_compute_apremio_substage`` reads first."""
    return types.SimpleNamespace(id=1, movements=movements)


def _decision_case(*, en_apremio: bool, apremio_substage: str | None):
    """Stub carrying only the DeadlineEngine-computed attributes the
    DecisionEngine reads, including the transient ``_apremio_substage``."""
    return types.SimpleNamespace(
        id=1,
        procedural_state=None,
        semaforo="rojo",
        abandono_disponible=False,
        prescripcion_cumplida=False,
        en_apremio=en_apremio,
        next_deadline_at=None,
        next_deadline_fatal=False,
        _apremio_substage=apremio_substage,
    )


# ---------------------------------------------------------------------------
# 1. Sub-stage detector
# ---------------------------------------------------------------------------
class TestComputeApremioSubstage:
    def test_stage_remate_returns_remate(self) -> None:
        case = _case_with_movements([_mv(stage="Remate", description="")])
        assert DeadlineEngine._compute_apremio_substage(None, case) == "remate"

    def test_description_martillero_returns_remate(self) -> None:
        case = _case_with_movements(
            [_mv(stage="Apremio", description="Designa martillero público")]
        )
        assert DeadlineEngine._compute_apremio_substage(None, case) == "remate"

    def test_description_subasta_returns_remate(self) -> None:
        case = _case_with_movements(
            [_mv(stage="Apremio", description="Fija fecha de subasta")]
        )
        assert DeadlineEngine._compute_apremio_substage(None, case) == "remate"

    def test_embargo_only_returns_none(self) -> None:
        case = _case_with_movements(
            [_mv(stage="Apremio", description="Se traba embargo sobre bien raíz")]
        )
        assert DeadlineEngine._compute_apremio_substage(None, case) is None

    def test_no_matching_movements_returns_none(self) -> None:
        case = _case_with_movements([_mv(stage="Gestión", description="Escrito")])
        assert DeadlineEngine._compute_apremio_substage(None, case) is None

    def test_never_raises_returns_none(self) -> None:
        # Bare object with no movements attr and no db → must swallow and None.
        assert DeadlineEngine._compute_apremio_substage(None, object()) is None


# ---------------------------------------------------------------------------
# 2. gestion_remate rule behaviour
# ---------------------------------------------------------------------------
class TestGestionRemateRule:
    def test_remate_substage_recommends_gestion_remate(self) -> None:
        case = _decision_case(en_apremio=True, apremio_substage="remate")
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "gestion_remate"
        assert rec.urgency == Urgency.CRITICA

    def test_generic_apremio_unchanged_when_no_substage(self) -> None:
        case = _decision_case(en_apremio=True, apremio_substage=None)
        rec = DecisionEngine.recommend(case, today=TODAY)
        assert rec is not None
        assert rec.code == "gestion_apremio"

    def test_remate_carries_no_deadline_field(self) -> None:
        """The rule is a recommendation only — it must not encode any
        deadline. DecisionRule has no deadline field; assert the resolved
        rule exposes none and recommend() sets no next_deadline_at."""
        rule = resolve_rule("gestion_remate")
        assert rule is not None
        assert not hasattr(rule, "next_deadline_at")
        assert not hasattr(rule, "deadline")

        case = _decision_case(en_apremio=True, apremio_substage="remate")
        rec = DecisionEngine.recommend(case, today=TODAY)
        # recommend() only reports the case's existing next_deadline_at (None
        # here); it never sets one on the case.
        assert rec.related_deadline_at is None
        assert getattr(case, "next_deadline_at", None) is None

    def test_compute_next_review_unaffected_by_substage(self) -> None:
        """Sub-stage must not change the standing-opportunity review date —
        an apremio case reviews on the same staggered horizon regardless."""
        base = _decision_case(en_apremio=True, apremio_substage=None)
        remate = _decision_case(en_apremio=True, apremio_substage="remate")
        assert DecisionEngine.compute_next_review_at(
            base, today=TODAY
        ) == DecisionEngine.compute_next_review_at(remate, today=TODAY)


# ---------------------------------------------------------------------------
# 3. resolve_rule + disclaimer wiring
# ---------------------------------------------------------------------------
class TestGestionRemateResolution:
    def test_resolve_rule_returns_gestion_remate(self) -> None:
        rule = resolve_rule("gestion_remate")
        assert isinstance(rule, DecisionRule)
        assert rule.code == "gestion_remate"
        assert "remate" in rule.action_text.lower()
        assert "486" in rule.legal_basis

    def test_disclaimer_constant_present(self) -> None:
        # The mandatory recommendation disclaimer still applies to this rule.
        assert RECOMMENDATION_DISCLAIMER
