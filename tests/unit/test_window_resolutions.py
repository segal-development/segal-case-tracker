"""``window_resolutions``: the court acts dictated inside the excepciones window.

A display of what the court ordered (not the plazo engine): no cutoff, no ownership check.
"""

from __future__ import annotations

from datetime import date, datetime

from app.services.poder_deadline import window_resolutions
from tests.fixtures.real_case_movements import FakeMovement

MERO = "Mero trámite"
TRASLADO = "Opone excepciones-Traslado"
TODAY = date(2026, 10, 10)


def _mv(day: str, description: str, procedure: str = "Resolución", id: int = 0) -> FakeMovement:
    return FakeMovement(
        stage="Excepciones",
        procedure=procedure,
        description=description,
        movement_date=datetime.strptime(day, "%Y-%m-%d"),
        id=id,
    )


def _opone(day: str = "2026-09-21") -> FakeMovement:
    return _mv(day, "Opone excepciones", procedure="Escrito", id=1)


def test_mero_tramite_followed_by_acredita_poder_is_cumplida() -> None:
    movements = [
        _opone(),
        _mv("2026-10-01", MERO, id=2),
        _mv("2026-10-05", "Acredita Poder", procedure="Escrito", id=3),
    ]
    assert window_resolutions(movements, TODAY) == [
        {
            "date": "2026-10-01",
            "description": MERO,
            "requires_action": True,
            "status": "cumplida",
            "due_date": "2026-10-05",
            "resolved_on": "2026-10-05",
            "resolved_by": "Acredita Poder",
        }
    ]


def test_unfulfilled_within_three_business_days_is_pendiente() -> None:
    result = window_resolutions([_opone(), _mv("2026-10-08", MERO, id=2)], TODAY)
    assert len(result) == 1
    assert result[0]["status"] == "pendiente"
    assert result[0]["due_date"] == "2026-10-13"
    assert result[0]["resolved_on"] is None
    assert result[0]["resolved_by"] is None


def test_due_day_itself_is_still_pendiente_and_next_day_is_vencida() -> None:
    movements = [_opone(), _mv("2026-10-01", MERO, id=2)]
    # 01/10 (Thu) + 3 business days (Saturday counts) = 05/10 (Mon)
    assert window_resolutions(movements, date(2026, 10, 5))[0]["status"] == "pendiente"
    assert window_resolutions(movements, date(2026, 10, 6))[0]["status"] == "vencida"


def test_court_going_straight_to_traslado_has_no_requires_action_item() -> None:
    movements = [_opone(), _mv("2026-09-25", TRASLADO, id=2)]
    result = window_resolutions(movements, TODAY)
    assert [r["requires_action"] for r in result] == [False]
    assert result[0]["status"] == "informativa"
    assert result[0]["due_date"] is None


def test_traslado_after_unfulfilled_trigger_is_avanzo() -> None:
    movements = [
        _opone(),
        _mv("2026-09-25", MERO, id=2),
        _mv("2026-09-30", TRASLADO, id=3),
    ]
    result = window_resolutions(movements, TODAY)
    assert [r["description"] for r in result] == [MERO, TRASLADO]
    assert result[0]["status"] == "avanzo"
    assert result[0]["resolved_on"] == "2026-09-30"
    assert result[0]["resolved_by"] is None
    assert result[1]["status"] == "informativa"


def test_previo_a_proveer_counts_as_requires_action() -> None:
    result = window_resolutions([_opone(), _mv("2026-10-08", "Previo a proveer", id=2)], TODAY)
    assert result[0]["requires_action"] is True
    assert result[0]["status"] == "pendiente"


def test_cumple_lo_ordenado_fulfils() -> None:
    movements = [
        _opone(),
        _mv("2026-10-01", "Apercibimiento poder y/o título", id=2),
        _mv("2026-10-02", "Cumple lo ordenado", procedure="Escrito", id=3),
    ]
    assert window_resolutions(movements, TODAY)[0]["status"] == "cumplida"


def test_nulo_movements_are_ignored() -> None:
    movements = [
        _opone(),
        _mv("2026-10-01", "[Nulo] Mero trámite", id=2),
        _mv("2026-10-02", "Previo a proveer", id=3),
        _mv("2026-10-03", "[Nulo] Acredita Poder", procedure="Escrito", id=4),
    ]
    result = window_resolutions(movements, TODAY)
    assert [r["description"] for r in result] == ["Previo a proveer"]
    assert result[0]["status"] != "cumplida"


def test_cap_keeps_the_last_eight_oldest_first() -> None:
    movements = [_opone()] + [
        _mv(f"2026-10-{d:02d}", f"Resolución {d}", id=10 + d) for d in range(1, 12)
    ]
    result = window_resolutions(movements, TODAY)
    assert [r["description"] for r in result] == [f"Resolución {d}" for d in range(4, 12)]


def test_informative_resolution_is_listed() -> None:
    movements = [_opone(), _mv("2026-10-02", "Confiere traslado de incidente", id=2)]
    result = window_resolutions(movements, TODAY)
    assert result == [
        {
            "date": "2026-10-02",
            "description": "Confiere traslado de incidente",
            "requires_action": False,
            "status": "informativa",
            "due_date": None,
            "resolved_on": None,
            "resolved_by": None,
        }
    ]


def test_each_trigger_is_evaluated_against_compliance_after_itself() -> None:
    movements = [
        _opone(),
        _mv("2026-10-01", MERO, id=2),
        _mv("2026-10-02", "Acredita Poder", procedure="Escrito", id=3),
        _mv("2026-10-03", "Previo a proveer", id=4),
    ]
    result = window_resolutions(movements, date(2026, 10, 20))
    assert [(r["description"], r["status"]) for r in result] == [
        (MERO, "cumplida"),
        ("Previo a proveer", "vencida"),
    ]


def test_unsorted_input_is_ordered_oldest_first() -> None:
    movements = [
        _mv("2026-10-05", "Acredita Poder", procedure="Escrito", id=3),
        _mv("2026-10-01", MERO, id=2),
        _opone(),
    ]
    result = window_resolutions(movements, TODAY)
    assert [(r["date"], r["status"]) for r in result] == [("2026-10-01", "cumplida")]


def test_acts_before_the_first_opone_excepciones_and_after_traslado_are_excluded() -> None:
    movements = [
        _mv("2026-09-01", "Resolución previa", id=5),
        _opone(),
        _mv("2026-09-30", TRASLADO, id=3),
        _mv("2026-10-02", "Resolución posterior", id=6),
        _mv("2026-10-03", TRASLADO, id=7),
    ]
    result = window_resolutions(movements, TODAY)
    assert [r["description"] for r in result] == [TRASLADO]


def test_escritos_and_the_opone_filing_are_not_listed() -> None:
    movements = [
        _opone(),
        _mv("2026-09-22", "Téngase presente", procedure="Escrito", id=2),
    ]
    assert window_resolutions(movements, TODAY) == []


def test_no_opone_excepciones_returns_empty() -> None:
    assert window_resolutions([_mv("2026-10-01", MERO, id=2)], TODAY) == []


def test_description_is_stripped() -> None:
    result = window_resolutions([_opone(), _mv("2026-10-01", "  Resuelvo algo  ", id=2)], TODAY)
    assert result[0]["description"] == "Resuelvo algo"
