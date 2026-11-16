"""es_activa truth table, the stored flag staying in step, and the exclusion split."""

from datetime import datetime

import pytest

from app.models.case import Case, compute_es_activa
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.causa_activa import (
    ACTIVAS_DEFINICION,
    CASE_ACTIVA_FILTER,
    excluidas_de,
    motivo_exclusion,
)


@pytest.mark.parametrize(
    "terminada,contrato,esperado",
    [
        (False, "activo", True),
        (False, "moroso", True),
        (False, "sin_dato", True),
        (False, None, True),
        (False, "caducado", False),
        (True, "activo", False),
        (True, "moroso", False),
        (True, "sin_dato", False),
        (True, None, False),
        (True, "caducado", False),
    ],
)
def test_es_activa_truth_table(terminada, contrato, esperado):
    assert compute_es_activa(terminada, contrato) is esperado


def test_definicion_text_is_verbatim():
    assert ACTIVAS_DEFINICION == "no terminada en Sysgal; contrato vigente, moroso o sin dato"


def test_motivo_exclusion_terminada_wins_over_caducado():
    assert motivo_exclusion(True, "caducado") == "terminada_sysgal"
    assert motivo_exclusion(False, "caducado") == "contrato_caducado"
    assert motivo_exclusion(False, "activo") is None
    assert motivo_exclusion(False, None) is None


def test_excluidas_de_counts_each_case_once():
    assert excluidas_de([(True, "caducado"), (False, "caducado"), (False, "activo"), (True, None)]) == {
        "terminada_sysgal": 2,
        "contrato_caducado": 1,
    }


@pytest.fixture
def case_factory(db):
    owner = Lawyer(rut="11111111-1", name="O", role="lawyer")
    court = Court(code="T1-CA", name="J", region="RM", type="civil")
    db.add_all([owner, court])
    db.commit()

    def make(rol, **fields):
        obj = Case(lawyer_id=owner.id, court_id=court.id, rol=rol, status="active", competencia="civil",
                   created_at=datetime.utcnow(), updated_at=datetime.utcnow(), **fields)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    return make


def test_stored_flag_follows_its_inputs_on_insert_and_update(db, case_factory):
    case = case_factory("C-1-2026", terminada_en_sysgal=True)
    assert case.es_activa is False

    case.terminada_en_sysgal = False
    db.commit()
    db.refresh(case)
    assert case.es_activa is True

    case.contrato_estado = "caducado"
    db.commit()
    db.refresh(case)
    assert case.es_activa is False

    case.contrato_estado = "moroso"
    db.commit()
    db.refresh(case)
    assert case.es_activa is True


def test_case_activa_filter_selects_the_stored_flag(db, case_factory):
    live = case_factory("C-1-2026")
    case_factory("C-2-2026", terminada_en_sysgal=True)
    case_factory("C-3-2026", contrato_estado="caducado")
    assert [c.id for c in db.query(Case).filter(CASE_ACTIVA_FILTER)] == [live.id]
