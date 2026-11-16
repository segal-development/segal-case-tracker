"""contrato_estado: precedence (Sysgal rut_cliente > DDO inference > sin_dato), derive rules, recompute."""

from datetime import date, datetime

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.services.contrato_estado import (
    compute_contrato_estados,
    recompute_contrato_estado,
)

TODAY = date(2026, 11, 16)


@pytest.fixture
def owner(db):
    obj = Lawyer(rut="11111111-1", name="Owner", role="lawyer")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@pytest.fixture
def court(db):
    obj = Court(code="T1-CE", name="Juzgado CE", region="RM", type="civil")
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


_seq = {"n": 0}


def _case(db, owner, court, **fields):
    _seq["n"] += 1
    obj = Case(lawyer_id=owner.id, court_id=court.id, rol=f"C-{_seq['n']}-2026", status="active",
               competencia="civil", created_at=datetime.utcnow(), updated_at=datetime(2026, 1, 1), **fields)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def _cache(db, rut, code, hasta=None, encontrado=True):
    db.add(ClienteSysgalEstado(rut=rut, encontrado=encontrado, estado_codigo=code, vigencia_hasta=hasta,
                               synced_at=datetime(2026, 11, 1)))
    db.commit()


def _causa(db, case, rut_cliente, estado="activa", eliminada=False, updated=datetime(2026, 11, 1)):
    _seq["n"] += 1
    db.add(SysgalCausa(sysgal_id=_seq["n"], rol="x", rol_norm="x", tribunal="t", rut_cliente=rut_cliente,
                       estado=estado, eliminada=eliminada, case_id=case.id, sysgal_updated_at=updated))
    db.commit()


def _ddo(db, case, rut, persona="NATURAL"):
    db.add(CaseLitigante(case_id=case.id, participante="DDO.", rut=rut, persona_type=persona, nombre="N",
                         natural_key=f"{case.id}-{rut}"))
    db.commit()


def test_sysgal_rut_cliente_wins_over_ddo_inference(db, owner, court):
    case = _case(db, owner, court)
    _causa(db, case, "11.111.111-1")
    _ddo(db, case, "22222222-2")
    _cache(db, "11111111-1", "TERMINADO")
    _cache(db, "22222222-2", "ACTIVO")
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "caducado"


def test_falls_back_to_ddo_when_no_sysgal_causa(db, owner, court):
    case = _case(db, owner, court)
    _ddo(db, case, "22222222-2")
    _cache(db, "22222222-2", "MOROSO_INACTIVO")
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "moroso"


def test_falls_back_to_ddo_when_rut_cliente_has_no_cache_row(db, owner, court):
    case = _case(db, owner, court)
    _causa(db, case, "11111111-1")
    _ddo(db, case, "22222222-2")
    _cache(db, "22222222-2", "ACTIVO", hasta=date(2027, 1, 1))
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "activo"


def test_juridica_ddo_is_not_the_client(db, owner, court):
    case = _case(db, owner, court)
    _ddo(db, case, "76123456-7", persona="JURIDICA")
    _cache(db, "76123456-7", "ACTIVO")
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "sin_dato"


def test_no_rut_at_all_is_sin_dato(db, owner, court):
    case = _case(db, owner, court)
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "sin_dato"


def test_stale_activo_becomes_caducado(db, owner, court):
    case = _case(db, owner, court)
    _causa(db, case, "11111111-1")
    _cache(db, "11111111-1", "ACTIVO", hasta=date(2026, 1, 1))
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "caducado"


def test_only_active_non_eliminada_causa_counts_and_latest_wins(db, owner, court):
    case = _case(db, owner, court)
    _causa(db, case, "11111111-1", updated=datetime(2026, 1, 1))
    _causa(db, case, "22222222-2", updated=datetime(2026, 6, 1))
    _causa(db, case, "33333333-3", estado="terminada", updated=datetime(2026, 9, 1))
    _causa(db, case, "44444444-4", eliminada=True, updated=datetime(2026, 10, 1))
    for rut, code in [("11111111-1", "ACTIVO"), ("22222222-2", "MOROSO_INACTIVO"),
                      ("33333333-3", "TERMINADO"), ("44444444-4", "TERMINADO")]:
        _cache(db, rut, code)
    assert compute_contrato_estados(db, today=TODAY)[case.id] == "moroso"


def test_recompute_writes_only_changes_sets_es_activa_and_keeps_other_fields(db, owner, court):
    cad = _case(db, owner, court, semaforo="rojo")
    ok = _case(db, owner, court, semaforo="verde")
    _causa(db, cad, "11111111-1")
    _cache(db, "11111111-1", "TERMINADO")

    report = recompute_contrato_estado(db, today=TODAY)
    db.expire_all()

    assert (cad.contrato_estado, cad.es_activa, cad.semaforo) == ("caducado", False, "rojo")
    assert (ok.contrato_estado, ok.es_activa, ok.semaforo) == ("sin_dato", True, "verde")
    assert cad.updated_at == datetime(2026, 1, 1)  # list order (updated_at) must not shuffle
    assert report["cambiadas"] == 2
    assert report["por_estado"] == {"activo": 0, "moroso": 0, "caducado": 1, "sin_dato": 1}

    again = recompute_contrato_estado(db, today=TODAY)
    assert again["cambiadas"] == 0


def test_recompute_dry_run_writes_nothing(db, owner, court):
    cad = _case(db, owner, court)
    _causa(db, cad, "11111111-1")
    _cache(db, "11111111-1", "TERMINADO")

    report = recompute_contrato_estado(db, today=TODAY, dry_run=True)
    db.expire_all()

    assert cad.contrato_estado is None and cad.es_activa is True
    assert report["por_estado"]["caducado"] == 1 and report["cambiadas"] == 1


def test_recompute_only_the_given_cases(db, owner, court):
    a = _case(db, owner, court)
    b = _case(db, owner, court)
    for c in (a, b):
        _causa(db, c, "11111111-1")
    _cache(db, "11111111-1", "TERMINADO")

    recompute_contrato_estado(db, case_ids=[a.id], today=TODAY)
    db.expire_all()
    assert a.contrato_estado == "caducado" and b.contrato_estado is None
