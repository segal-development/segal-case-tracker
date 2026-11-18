"""Sysgal cuotas: sync storage + mora derivation."""

from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_contrato import SysgalContrato
from app.services.sysgal_client import (
    SysgalContratoCuotasDTO as DTO,
    SysgalCuotasResult,
    SysgalError,
)
from app.services.sysgal_cuotas import (
    MORA_BLOQUEA,
    MORA_NO_BLOQUEA,
    MORA_SIN_INFO,
    cuotas_actualizadas_al,
    derive_mora,
    load_mora_por_case,
    sync_sysgal_cuotas,
)

COPIA = datetime(2026, 11, 18, 7, 0)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def cuotas_contratos(self):
        if self.error:
            raise self.error
        return self.result


def _dto(contrato, cuota=1, moroso=True, causas=(10,)):
    return DTO(str(contrato), cuota, moroso, not moroso, list(causas))


def _rows(db):
    return {r.contrato: r for r in db.query(SysgalContrato).all()}


# ---- storage ----
def test_sync_inserts_then_upserts_and_removes_absent(db):
    first = [_dto(1), _dto(2, causas=(20, 21))] + [_dto(i) for i in range(10, 18)]
    sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, first)))
    db.commit()
    rows = _rows(db)
    assert len(rows) == 10
    assert rows["2"].id_causas == [20, 21] and rows["1"].copia_at == COPIA

    new = datetime(2026, 11, 19, 7, 0)
    second = [_dto(2, cuota=4, moroso=False, causas=(20,)), _dto(3)] + [_dto(i) for i in range(10, 17)]
    stats = sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, new, second)))
    db.commit()
    rows = _rows(db)
    assert "1" not in rows and "17" not in rows and "3" in rows  # 1 and 17 absent -> removed (20%)
    assert (rows["2"].cuota_actual, rows["2"].contrato_moroso, rows["2"].contrato_al_dia) == (4, False, True)
    assert rows["2"].id_causas == [20] and rows["2"].copia_at == new
    assert stats["insertados"] == 1 and stats["actualizados"] == 8 and stats["eliminados"] == 2


def test_failed_sync_changes_nothing(db):
    sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [_dto(1)])))
    db.commit()
    with pytest.raises(SysgalError):
        sync_sysgal_cuotas(db, FakeClient(error=SysgalError("boom")))
    db.rollback()
    assert set(_rows(db)) == {"1"}


def test_encontrado_false_changes_nothing(db):
    sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [_dto(1)])))
    db.commit()
    stats = sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(False, None, [])))
    db.commit()
    assert set(_rows(db)) == {"1"} and stats["encontrado"] is False


def _seed(db, n):
    sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [_dto(i) for i in range(n)])))
    db.commit()


def test_empty_copy_writes_nothing(db):
    _seed(db, 3)
    stats = sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [])))
    db.commit()
    assert len(_rows(db)) == 3
    assert stats["eliminados"] == 0 and stats["omitido"]


def test_mass_deletion_over_20_percent_is_refused(db, caplog):
    _seed(db, 10)
    # keeps 7 (+ one new): 3 removals = 30% > 20%
    with caplog.at_level("WARNING"):
        stats = sync_sysgal_cuotas(
            db, FakeClient(SysgalCuotasResult(True, datetime(2026, 11, 19), [_dto(i, cuota=5, moroso=False) for i in range(7)] + [_dto(99)]))
        )
    db.commit()
    rows = _rows(db)
    assert set(rows) == {str(i) for i in range(10)}  # nothing inserted, updated or removed
    assert rows["0"].cuota_actual == 1 and rows["0"].copia_at == COPIA
    assert stats["omitido"] and stats["eliminados"] == 0 and stats["insertados"] == 0
    assert "3" in caplog.text and "10" in caplog.text


def test_deletion_up_to_20_percent_is_applied(db):
    _seed(db, 10)
    stats = sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [_dto(i) for i in range(8)])))
    db.commit()
    assert len(_rows(db)) == 8 and stats["eliminados"] == 2 and not stats.get("omitido")


def test_cuotas_actualizadas_al(db):
    assert cuotas_actualizadas_al(db) is None
    sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [_dto(1)])))
    db.commit()
    assert cuotas_actualizadas_al(db) == COPIA


# ---- pure derivation ----
@pytest.mark.parametrize(
    "cuota,moroso,expected",
    [(1, True, MORA_BLOQUEA), (2, True, MORA_BLOQUEA), (3, True, MORA_NO_BLOQUEA),
     (None, True, MORA_NO_BLOQUEA), (1, False, MORA_NO_BLOQUEA), (2, False, MORA_NO_BLOQUEA)],
)
def test_derive_cuota_rules(cuota, moroso, expected):
    assert derive_mora({1: {10}}, [_dto(1, cuota, moroso, (10,))]) == {1: expected}


def test_derive_no_contract_info_is_unknown_not_blocked():
    assert derive_mora({1: {10}}, [_dto(1, 1, True, (99,))]) == {1: MORA_SIN_INFO}


def test_derive_several_causas_per_contract_and_contracts_per_causa():
    contratos = [
        _dto(1, 1, True, (10, 11)),
        _dto(2, 5, False, (11,)),
        _dto(3, 3, False, (12,)),
    ]
    got = derive_mora({1: {10}, 2: {11}, 3: {12}, 4: {10, 12}, 5: {13}}, contratos)
    assert got[1] == MORA_BLOQUEA and got[2] == MORA_BLOQUEA  # 11 in a blocking + a non-blocking contract
    assert got[3] == MORA_NO_BLOQUEA
    assert got[4] == MORA_BLOQUEA  # any causa of the case
    assert got[5] == MORA_SIN_INFO


def test_derive_case_without_causas_is_unknown():
    assert derive_mora({1: set()}, [_dto(1)]) == {1: MORA_SIN_INFO}


# ---- DB batch ----
def test_load_mora_por_case(db):
    lawyer = Lawyer(rut="16021492-9", name="F", is_active=True)
    court = Court(code="CU1", name="J", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    cases = []
    for i in range(3):
        c = Case(lawyer_id=lawyer.id, court_id=court.id, rol=f"C-{i}-2026", status="active")
        db.add(c)
        db.commit()
        cases.append(c)
    for sid, c, elim in ((10, cases[0], False), (20, cases[1], False), (30, cases[2], True)):
        db.add(SysgalCausa(sysgal_id=sid, rol="r", rol_norm="r", tribunal="t", case_id=c.id,
                           eliminada=elim, synced_at=datetime(2026, 11, 1)))
    db.commit()
    sync_sysgal_cuotas(db, FakeClient(SysgalCuotasResult(True, COPIA, [
        _dto(1, 1, True, (10,)), _dto(2, 5, False, (20,)), _dto(3, 1, True, (30,))])))
    db.commit()
    got = load_mora_por_case(db, [c.id for c in cases])
    assert got == {cases[0].id: MORA_BLOQUEA, cases[1].id: MORA_NO_BLOQUEA, cases[2].id: MORA_SIN_INFO}
    assert load_mora_por_case(db, []) == {}


# ---- estado de pago (S9) ----
from sqlalchemy import event  # noqa: E402

from app.services.sysgal_cuotas import EstadoPago, estado_pago_por_case  # noqa: E402


def _pago_world(db, spec):
    """spec: {sysgal_id: case_idx}; returns cases. One lawyer/court."""
    lawyer = Lawyer(rut="16021492-9", name="L", is_active=True)
    court = Court(code="EP1", name="J", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    cases = []
    for i in range(max(spec.values()) + 1):
        c = Case(lawyer_id=lawyer.id, court_id=court.id, rol=f"C-{i}-2026", status="active")
        db.add(c)
        cases.append(c)
    db.commit()
    for sid, idx in spec.items():
        db.add(SysgalCausa(sysgal_id=sid, rol="r", rol_norm="r", tribunal="t",
                           case_id=cases[idx].id, synced_at=datetime(2026, 11, 1)))
    db.commit()
    return cases


def _con(db, n, causas, cuota, moroso=False, al_dia=False, copia=COPIA):
    db.add(SysgalContrato(contrato=str(n), cuota_actual=cuota, contrato_moroso=moroso,
                          contrato_al_dia=al_dia, id_causas=list(causas), copia_at=copia,
                          synced_at=datetime(2026, 11, 18, 9)))
    db.commit()


@pytest.mark.parametrize(
    "cuota,moroso,al_dia,estado,bloquea",
    [
        (1, True, False, "moroso", True),
        (2, True, False, "moroso", True),
        (3, True, False, "moroso", False),
        (None, True, False, "moroso", False),
        (1, False, True, "al_dia", False),
        (2, False, False, "pendiente", False),
    ],
)
def test_estado_pago_per_contract(db, cuota, moroso, al_dia, estado, bloquea):
    (c,) = _pago_world(db, {10: 0})
    _con(db, 1, [10], cuota, moroso, al_dia)
    got = estado_pago_por_case(db, [c.id])
    assert got == {c.id: EstadoPago(estado, cuota, bloquea, COPIA)}


def test_estado_pago_worst_contract_wins_and_reports_its_cuota(db):
    c1, c2, c3 = _pago_world(db, {10: 0, 11: 0, 20: 1, 21: 1, 30: 2})
    _con(db, 1, [10], 5, al_dia=True)
    _con(db, 2, [11], 4)                       # pendiente
    _con(db, 3, [20], 5, al_dia=True)
    _con(db, 4, [21], 1, moroso=True)          # moroso beats al_dia
    _con(db, 5, [30], 7, al_dia=True)
    got = estado_pago_por_case(db, [c1.id, c2.id, c3.id])
    assert got[c1.id] == EstadoPago("pendiente", 4, False, COPIA)
    assert got[c2.id] == EstadoPago("moroso", 1, True, COPIA)
    assert got[c3.id].estado == "al_dia"


def test_estado_pago_moroso_prefers_the_blocking_contract(db):
    (c,) = _pago_world(db, {10: 0, 11: 0})
    _con(db, 1, [10], 3, moroso=True)
    _con(db, 2, [11], 2, moroso=True)
    assert estado_pago_por_case(db, [c.id])[c.id] == EstadoPago("moroso", 2, True, COPIA)


def test_estado_pago_sin_informacion_vs_no_causa_and_deleted_causa(db):
    c_sin, c_nocausa, c_del = _pago_world(db, {10: 0, 30: 2})
    db.query(SysgalCausa).filter_by(sysgal_id=30).update({"eliminada": True})
    db.commit()
    _con(db, 1, [30], 1, moroso=True)  # only reaches the deleted causa
    got = estado_pago_por_case(db, [c_sin.id, c_nocausa.id, c_del.id])
    assert got == {c_sin.id: EstadoPago("sin_informacion", None, False, None)}
    assert estado_pago_por_case(db, []) == {}


def test_estado_pago_query_count_does_not_depend_on_cases(db):
    cases = _pago_world(db, {100 + i: i for i in range(12)})
    for i in range(12):
        _con(db, i, [100 + i], 1, moroso=True)

    def count(ids):
        n = []
        eng = db.get_bind()
        cb = lambda *a, **k: n.append(1)  # noqa: E731
        event.listen(eng, "before_cursor_execute", cb)
        try:
            estado_pago_por_case(db, ids)
        finally:
            event.remove(eng, "before_cursor_execute", cb)
        return len(n)

    assert count([cases[0].id]) == count([c.id for c in cases]) <= 3
