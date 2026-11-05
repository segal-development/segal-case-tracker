"""stage_history: the date each procedural stage was reached.

The date of a stage is the ``movement_date`` of the movement that advanced the
classifier's state to it (see MovementClassifier._classify).
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from app.core.deadlines_config import ProceduralState
from app.services.procedural_classifier import MovementClassifier
from tests.fixtures.real_case_movements import FakeMovement

TODAY = date(2026, 6, 16)

NOTIF = "NOTIFICACIÓN DE DEMANDA (Exitosa)"
PRUEBA = "Notificación resolución que recibe la causa a prue (Exitosa)"


def _mv(date_str: str, stage: str, description: str) -> FakeMovement:
    return FakeMovement(
        stage=stage,
        procedure="",
        description=description,
        movement_date=datetime.strptime(date_str, "%Y-%m-%d"),
    )


def _d(date_str: str) -> date:
    return date.fromisoformat(date_str)


def test_normal_flow_returns_stages_in_order_with_trigger_dates() -> None:
    movements = [
        _mv("2026-04-09", "Gestión Preparatoria", NOTIF),
        _mv("2026-04-13", "Excepciones", "Opone excepciones"),
        _mv("2026-05-06", "Contestación Excepciones", "Evacúa traslado"),
    ]
    history = MovementClassifier().stage_history(movements, TODAY)
    assert history == [
        (ProceduralState.NOTIFICADO, _d("2026-04-09")),
        (ProceduralState.EXCEPCIONES, _d("2026-04-13")),
        (ProceduralState.TRASLADO_EJECUTANTE, _d("2026-05-06")),
    ]


def test_input_order_does_not_matter() -> None:
    movements = [
        _mv("2026-04-13", "Excepciones", "Opone excepciones"),
        _mv("2026-04-09", "Gestión Preparatoria", NOTIF),
    ]
    history = MovementClassifier().stage_history(movements, TODAY)
    assert [s for s, _ in history] == [
        ProceduralState.NOTIFICADO,
        ProceduralState.EXCEPCIONES,
    ]


def test_skipped_stage_is_absent() -> None:
    movements = [
        _mv("2026-04-09", "Gestión Preparatoria", NOTIF),
        _mv("2026-05-30", "Contestación Excepciones", PRUEBA),
    ]
    history = MovementClassifier().stage_history(movements, TODAY)
    assert history == [
        (ProceduralState.NOTIFICADO, _d("2026-04-09")),
        (ProceduralState.AUTO_PRUEBA, _d("2026-05-30")),
    ]


def test_old_stage_movement_after_later_stage_does_not_move_the_date() -> None:
    movements = [
        _mv("2026-04-09", "Gestión Preparatoria", NOTIF),
        _mv("2026-04-13", "Excepciones", "Opone excepciones"),
        _mv("2026-05-06", "Contestación Excepciones", "Evacúa traslado"),
        _mv("2026-05-20", "Excepciones", "Opone excepciones"),
    ]
    history = dict(MovementClassifier().stage_history(movements, TODAY))
    assert history[ProceduralState.EXCEPCIONES] == _d("2026-04-13")
    assert len(history) == 3


def test_annulled_movement_never_advances() -> None:
    movements = [
        _mv("2026-04-09", "Gestión Preparatoria", NOTIF),
        _mv("2026-05-30", "Contestación Excepciones", "[Nulo] " + PRUEBA),
    ]
    history = MovementClassifier().stage_history(movements, TODAY)
    assert history == [(ProceduralState.NOTIFICADO, _d("2026-04-09"))]


def test_empty_movements_return_empty_list() -> None:
    assert MovementClassifier().stage_history([], TODAY) == []


def test_no_matching_movement_returns_empty_list() -> None:
    movements = [_mv("2026-04-09", "Otro", "Escrito cualquiera")]
    assert MovementClassifier().stage_history(movements, TODAY) == []


def test_same_day_movements_both_recorded_with_same_date() -> None:
    movements = [
        _mv("2026-04-09", "Gestión Preparatoria", NOTIF),
        _mv("2026-04-09", "Excepciones", "Opone excepciones"),
    ]
    history = MovementClassifier().stage_history(movements, TODAY)
    assert history == [
        (ProceduralState.NOTIFICADO, _d("2026-04-09")),
        (ProceduralState.EXCEPCIONES, _d("2026-04-09")),
    ]


def test_date_valued_movement_dates_are_supported() -> None:
    mv = SimpleNamespace(
        stage="Gestión Preparatoria",
        description=NOTIF,
        movement_date=_d("2026-04-09"),
    )
    history = MovementClassifier().stage_history([mv], TODAY)
    assert history == [(ProceduralState.NOTIFICADO, _d("2026-04-09"))]


def test_safe_fails_to_empty_list_on_error() -> None:
    class Broken:
        stage = "x"
        description = "y"
        movement_date = None  # sorting a single item is fine; two Nones are not

    assert MovementClassifier().stage_history([Broken(), Broken()], TODAY) == []
