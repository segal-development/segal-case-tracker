"""reclassify_cases recomputes the semáforo of the cases whose ``terminada_en_sysgal`` flips.

The engine exempts a terminada abandono from the forced rojo; when Sysgal reactivates the
causa nothing else would reapply the rule, so the case would stay verde.
"""
from datetime import date, datetime

import pytest
from dateutil.relativedelta import relativedelta

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.sysgal_causa import SysgalCausa
from app.services.deadline_engine import DeadlineEngine
from app.services.matriz_sysgal import reclassify_cases

TODAY = date(2026, 11, 16)
MOTIVO_ABANDONO = "Abandono disponible: presentar el escrito"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut="11111111-1", email="a@t.cl", name="Ana Abogada", is_active=True)
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def court(db):
    obj = Court(name="12º Juzgado Civil de Santiago", code="C012", region="RM", type="civil")
    db.add(obj)
    db.commit()
    return obj


def _abandono_case(db, lawyer, court, **kw) -> Case:
    """Post-sentencia, last movement 3.5 years ago: abandono_disponible; stored green."""
    case = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-1-2026", competencia="civil",
                procedure="Ejecutivo", status="active", filed_at=datetime(2020, 1, 1),
                semaforo="verde", **kw)
    db.add(case)
    db.flush()
    for delta, stage, desc in [
        (relativedelta(years=5), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)"),
        (relativedelta(years=4), "Tramitación", "Citación a oír sentencia"),
        (relativedelta(months=42), "Tramitación", "Dicta Sentencia Definitiva"),
    ]:
        when = datetime.combine(TODAY - delta, datetime.min.time())
        db.add(Movement(case_id=case.id, stage=stage, procedure="", description=desc, movement_date=when))
    db.commit()
    return case


_sid = iter(range(1, 1000))


def _link(db, case, estado):
    row = SysgalCausa(sysgal_id=next(_sid), rol="C-1-2026", rol_norm="C-1-2026", tribunal="x",
                      etapa="APREMIO", estado=estado, eliminada=False, case_id=case.id)
    db.add(row)
    db.commit()
    return row


def test_reactivated_abandono_causa_turns_rojo_with_motivo(db, lawyer, court):
    case = _abandono_case(db, lawyer, court, terminada_en_sysgal=True)
    _link(db, case, "activa")

    reclassify_cases(db, {case.id})
    db.refresh(case)

    assert case.terminada_en_sysgal is False
    assert case.semaforo == "rojo"
    assert case.semaforo_motivo == MOTIVO_ABANDONO


def test_flip_to_terminada_recomputes_without_forcing_rojo(db, lawyer, court):
    case = _abandono_case(db, lawyer, court, terminada_en_sysgal=False)
    case.semaforo, case.semaforo_motivo = "rojo", MOTIVO_ABANDONO
    db.commit()
    _link(db, case, "terminada")

    reclassify_cases(db, {case.id})
    db.refresh(case)

    assert case.terminada_en_sysgal is True
    assert case.semaforo != "rojo"
    assert case.semaforo_motivo is None


def test_unchanged_flag_is_not_recomputed(db, lawyer, court, monkeypatch):
    case = _abandono_case(db, lawyer, court, terminada_en_sysgal=False)
    _link(db, case, "activa")
    calls = []
    monkeypatch.setattr(DeadlineEngine, "recompute_case", lambda *a, **k: calls.append(a))

    assert reclassify_cases(db, {case.id}) == 1
    db.refresh(case)

    assert calls == []
    assert case.semaforo == "verde"


def test_engine_failure_does_not_break_reclassify(db, lawyer, court, monkeypatch):
    case = _abandono_case(db, lawyer, court, terminada_en_sysgal=True)
    _link(db, case, "activa")

    def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(DeadlineEngine, "recompute_case", boom)

    assert reclassify_cases(db, {case.id}) == 1
    db.refresh(case)

    assert case.terminada_en_sysgal is False  # flag still committed
