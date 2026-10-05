"""The FATAL excepciones plazo must anchor to the act, not to PJUD's publication.

Real data (QA, 3,985 successful notifications): every one carries
``Diligencia:DD/MM/YYYY HH:MM`` in its description; PJUD published after the
diligencia in 64% of them (4 days on average), never before.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.deadlines_config import DeadlineType, ProceduralState
from app.services.business_days import add_business_days
from app.services.deadline_engine import DeadlineEngine
from tests.fixtures.real_case_movements import FakeMovement

TODAY = date(2026, 6, 16)


def _mv(published: str, description: str, stage: str = "Gestión Preparatoria") -> FakeMovement:
    return FakeMovement(
        stage=stage,
        procedure="Actuación Receptor",
        description=description,
        movement_date=datetime.strptime(published, "%Y-%m-%d"),
    )


def _classify(movements):
    from app.services.procedural_classifier import MovementClassifier

    return MovementClassifier().classify(movements, TODAY)


class TestAnchor:
    def test_diligencia_before_publication_anchors_the_deadline(self) -> None:
        mv = _mv("2025-11-26", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:22/11/2025 12:21")
        assert DeadlineEngine._triggered_at_date(mv) == date(2025, 11, 22)

    def test_without_diligencia_falls_back_to_movement_date(self) -> None:
        mv = _mv("2025-11-26", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        assert DeadlineEngine._triggered_at_date(mv) == date(2025, 11, 26)

    @pytest.mark.parametrize(
        "desc",
        [
            "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:31/02/2025 12:21",  # impossible date
            "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:ayer",  # garbage
            "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:",  # empty
            "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:2025-11-22",  # wrong format
        ],
    )
    def test_malformed_diligencia_falls_back_without_raising(self, desc: str) -> None:
        assert DeadlineEngine._triggered_at_date(_mv("2025-11-26", desc)) == date(2025, 11, 26)

    def test_diligencia_after_publication_is_ignored(self) -> None:
        """A diligencia later than the publication is a data error: never push a deadline later."""
        mv = _mv("2025-11-26", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:30/11/2025 12:21")
        assert DeadlineEngine._triggered_at_date(mv) == date(2025, 11, 26)

    def test_precomputed_date_passes_through(self) -> None:
        assert DeadlineEngine._triggered_at_date(date(2025, 1, 2)) == date(2025, 1, 2)

    def test_late_publication_yields_already_expired_due_date(self) -> None:
        """The 160 cases: published >8 business days after the diligencia."""
        mv = _mv("2026-06-10", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:20/05/2026 10:00")
        anchored = DeadlineEngine._triggered_at_date(mv)
        due = add_business_days(anchored, DeadlineType.EXCEPCIONES_8D.dias_habiles)
        assert due < TODAY  # already expired
        # What the old anchor would have shown: alive and counting.
        assert add_business_days(date(2026, 6, 10), 8) >= TODAY

    def test_prueba_derived_deadline_uses_same_anchor(self) -> None:
        """OBSERVACIONES_PRUEBA_6D is derived from the probatorio anchor; keep them consistent."""
        mvs = [
            _mv("2026-04-01", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:30/03/2026 08:10"),
            _mv(
                "2026-06-03",
                "Notificación resolución que recibe la causa a prue (Exitosa) Diligencia:02/06/2026 10:00",
                stage="Contestación Excepciones",
            ),
        ]
        state, triggers = _classify(mvs)
        assert state == ProceduralState.AUTO_PRUEBA
        expected = add_business_days(date(2026, 6, 2), DeadlineType.TERMINO_PROBATORIO_10D.dias_habiles)
        assert triggers[DeadlineType.OBSERVACIONES_PRUEBA_6D] == expected


class TestTriggerMatching:
    def test_realizada_triggers_excepciones(self) -> None:
        state, triggers = _classify(
            [_mv("2026-04-09", "NOTIFICACIÓN DE DEMANDA (Realizada) Diligencia:08/04/2026 11:47")]
        )
        assert state == ProceduralState.NOTIFICADO
        assert DeadlineType.EXCEPCIONES_8D in triggers

    @pytest.mark.parametrize(
        "desc",
        [
            "NOTIFICACIÓN DE DEMANDA (Certificación) Diligencia:20/03/2026 12:03",
            "NOTIFICACIÓN DE DEMANDA (Búsqueda negativa) Diligencia:20/03/2026 12:03",
            "Notificación demanda (Búsqueda negativa) Diligencia:20/03/2026 12:03",
            "Certificación (Realizada) Diligencia:20/03/2026 12:03",
        ],
    )
    def test_failed_notifications_never_trigger(self, desc: str) -> None:
        state, triggers = _classify([_mv("2026-03-23", desc)])
        assert DeadlineType.EXCEPCIONES_8D not in triggers
        assert state != ProceduralState.NOTIFICADO
