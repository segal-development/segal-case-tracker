"""scripts/recalcular_contrato_estado.py core: dry-run writes nothing, the real run writes only changes."""
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.cliente_sysgal_estado import ClienteSysgalEstado
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from scripts.recalcular_contrato_estado import _print_report, compute


@pytest.fixture
def cases(db):
    owner = Lawyer(rut="11111111-1", name="O", role="lawyer")
    court = Court(code="T1-RC", name="J", region="RM", type="civil")
    db.add_all([owner, court])
    db.commit()
    out = []
    for n in range(3):
        c = Case(lawyer_id=owner.id, court_id=court.id, rol=f"C-{n}-2026", status="active", competencia="civil",
                 created_at=datetime.utcnow(), updated_at=datetime(2026, 1, 1), semaforo="rojo")
        db.add(c)
        out.append(c)
    db.commit()
    db.add(ClienteSysgalEstado(rut="11111111-1", encontrado=True, estado_codigo="TERMINADO", synced_at=datetime.utcnow()))
    db.add(SysgalCausa(sysgal_id=1, rol="x", rol_norm="x", tribunal="t", rut_cliente="11111111-1", estado="activa",
                       eliminada=False, case_id=out[0].id))
    db.commit()
    return out


def test_dry_run_reports_counts_and_writes_nothing(db, cases, capsys):
    result = compute(db, dry_run=True)
    _print_report(result, True)
    db.expire_all()

    assert result["por_estado"] == {"activo": 0, "moroso": 0, "caducado": 1, "sin_dato": 2}
    assert result["activas_antes"] == 3 and result["activas_despues"] == 2
    assert all(c.contrato_estado is None and c.es_activa is True for c in cases)
    out = capsys.readouterr().out
    assert "simulado" in out and "caducado" in out


def test_real_run_writes_only_the_changes_and_keeps_other_fields(db, cases):
    compute(db, dry_run=False)
    db.expire_all()

    assert [c.contrato_estado for c in cases] == ["caducado", "sin_dato", "sin_dato"]
    assert [c.es_activa for c in cases] == [False, True, True]
    assert all(c.semaforo == "rojo" and c.updated_at == datetime(2026, 1, 1) for c in cases)
    assert compute(db, dry_run=False)["cambiadas"] == 0
