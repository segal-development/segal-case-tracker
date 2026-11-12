"""Bono V3 fed by the Sysgal avance-de-cartera snapshot (S5); months without one stay manual."""

from datetime import datetime

import pytest

from app.core.security import create_access_token
from app.models.avance_cartera import AvanceCarteraSnapshot
from app.models.bono import BonoVariables
from app.models.lawyer import Lawyer
from app.models.sysgal_usuario import SysgalUsuario

ADMIN_RUT = "16021492-9"
NOTE = "Sin datos en Sysgal"


def _h():
    return {"Authorization": "Bearer " + create_access_token({"sub": ADMIN_RUT})}


@pytest.fixture
def setup(db):
    db.add(Lawyer(rut=ADMIN_RUT, name="Carla Admin", role="admin"))
    a = Lawyer(rut="11111111-1", name="Ana Junior", role="lawyer", nivel="junior")
    b = Lawyer(rut="22222222-2", name="Beto Pleno", role="lawyer", nivel="pleno")
    c = Lawyer(rut="33333333-3", name="Cami Junior", role="lawyer", nivel="junior")
    db.add_all([a, b, c])
    db.commit()
    db.add_all([
        SysgalUsuario(sysgal_id=1, nombre="Ana", lawyer_id=a.id, synced_at=datetime.utcnow()),
        SysgalUsuario(sysgal_id=2, nombre="Beto", lawyer_id=b.id, synced_at=datetime.utcnow()),
        SysgalUsuario(sysgal_id=3, nombre="Cami", lawyer_id=c.id, synced_at=datetime.utcnow()),
    ])
    # manual weeks that would give 95% for everyone in 2026-09 (no snapshot), 2026-10 (a snapshot
    # there must be ignored) and 2026-11 (snapshot)
    for periodo in ("2026-09", "2026-10", "2026-11"):
        for lw in (a, b, c):
            db.add(BonoVariables(lawyer_id=lw.id, periodo=periodo, nivel=lw.nivel, cumpl_sem1=95.0))
    db.commit()
    return a, b, c


def _snapshot(db, abogados, periodo="2026-11"):
    desde = datetime.strptime(periodo + "-01", "%Y-%m-%d").date()
    db.add(AvanceCarteraSnapshot(
        periodo=periodo, desde=desde, hasta=desde.replace(day=28),
        payload={"por_abogado": [{"abogado_id": i, "total": 10, "gestionadas": 5, "cobertura": c}
                                 for i, c in abogados]},
    ))
    db.commit()


def _rows(client, periodo):
    r = client.get(f"/api/v1/bono/liquidacion?periodo={periodo}", headers=_h())
    assert r.status_code == 200
    return {row["lawyer_nombre"]: row for row in r.json()["rows"]}


def test_snapshot_cobertura_drives_v3(client, db, setup):
    _snapshot(db, [(1, 91.5), (2, 82.0), (3, 50.0)])
    rows = _rows(client, "2026-11")
    ana, beto, cami = rows["Ana Junior"], rows["Beto Pleno"], rows["Cami Junior"]
    assert (ana["fuente_v3"], ana["cobertura_v3"], ana["nota_v3"]) == ("sysgal", 91.5, None)
    assert ana["v3_pct_cumplimiento"] == pytest.approx(0.915) and ana["v3_tramo_bruto"] == 100000
    assert beto["v3_pct_cumplimiento"] == pytest.approx(0.82) and beto["v3_tramo_bruto"] == 64615
    assert cami["v3_tramo_bruto"] == 0 and cami["v3_neta"] == 0
    # manual inputs are still reported untouched
    assert ana["cumpl_total"] == 95.0


def test_mapped_lawyer_absent_from_snapshot_is_zero_with_note(client, db, setup):
    _snapshot(db, [(1, 91.5)])
    cami = _rows(client, "2026-11")["Cami Junior"]
    assert cami["fuente_v3"] == "sysgal" and cami["cobertura_v3"] == 0
    assert cami["nota_v3"] == NOTE
    assert cami["v3_pct_cumplimiento"] == 0 and cami["v3_tramo_bruto"] == 0 and cami["v3_neta"] == 0


def test_month_without_snapshot_stays_manual(client, db, setup):
    _snapshot(db, [(1, 10.0), (2, 10.0), (3, 10.0)])  # snapshot is for 2026-11 only
    ana = _rows(client, "2026-09")["Ana Junior"]
    assert ana["fuente_v3"] == "manual" and ana["cobertura_v3"] is None and ana["nota_v3"] is None
    assert ana["v3_pct_cumplimiento"] == pytest.approx(0.95)
    assert ana["v3_tramo_bruto"] == 100000 and ana["v3_neta"] == 100000


def test_tramo_bands_unchanged_at_edges(client, db, setup):
    _snapshot(db, [(1, 90.0), (2, 79.99), (3, 80.0)])
    rows = _rows(client, "2026-11")
    assert rows["Ana Junior"]["v3_tramo_bruto"] == 100000
    assert rows["Beto Pleno"]["v3_tramo_bruto"] == 0
    assert rows["Cami Junior"]["v3_tramo_bruto"] == 80000


def test_totals_follow_snapshot(client, db, setup):
    _snapshot(db, [(1, 95.0), (2, 95.0), (3, 0.0)])
    body = client.get("/api/v1/bono/liquidacion?periodo=2026-11", headers=_h()).json()
    assert body["totales"]["v3_neta"] == 100000 + 80769


def test_upsert_variables_row_uses_snapshot_too(client, db, setup):
    a, _b, _c = setup
    _snapshot(db, [(1, 85.0)])
    r = client.put(
        f"/api/v1/bono/variables/{a.id}?periodo=2026-11", headers=_h(),
        json={"clientes_m2": 0, "clientes_activos": 0, "causas_asignadas": 0, "causas_cumplidas": 0,
              "reclamos_leve": 0, "reclamos_medio": 0, "reclamos_grave": 0, "renovaciones": 0,
              "cumpl_sem1": 95.0},
    )
    assert r.status_code == 200
    assert r.json()["fuente_v3"] == "sysgal" and r.json()["v3_tramo_bruto"] == 80000


def test_periodo_without_snapshot_equals_hand_computed_manual_path(client, db, setup):
    """Pre-feature liquidación, by hand: junior fijo + V1 + V3 (weeks) - V4, no Sysgal involved."""
    a, _b, _c = setup
    var = db.query(BonoVariables).filter_by(lawyer_id=a.id, periodo="2026-09").one()
    var.clientes_m2, var.clientes_activos = 100, 80  # 80% → $8.000 por cliente
    var.cumpl_sem1, var.cumpl_sem2, var.cumpl_sem3 = 40.0, 30.0, 20.0  # 90% → tramo 100.000
    var.reclamos_leve = 1  # -5% sobre V3
    db.commit()
    _snapshot(db, [(1, 0.0)])  # snapshot of ANOTHER month must not matter

    ana = _rows(client, "2026-09")["Ana Junior"]

    assert ana["fuente_v3"] == "manual"
    v1 = 80 * 8000
    v3 = round(100000 * 0.95)
    assert (ana["v1_bruto"], ana["v3_tramo_bruto"], ana["v3_neta"]) == (v1, 100000, v3)
    assert ana["total_bruto"] == 1_105_354 + v1 + v3


def test_snapshot_before_foto_desde_is_ignored_by_liquidacion(client, db, setup):
    """S11: a 2026-10 row inserted directly never feeds V3; the manual weeks do, as before."""
    _snapshot(db, [(1, 10.0), (2, 10.0), (3, 10.0)], periodo="2026-10")
    rows = _rows(client, "2026-10")
    ana, beto, cami = rows["Ana Junior"], rows["Beto Pleno"], rows["Cami Junior"]
    for row in (ana, beto, cami):
        assert row["fuente_v3"] == "manual" and row["cobertura_v3"] is None and row["nota_v3"] is None
        assert row["v3_pct_cumplimiento"] == pytest.approx(0.95)
    manual = _rows(client, "2026-09")  # same manual weeks, no snapshot at all
    for name, row in (("Ana Junior", ana), ("Beto Pleno", beto), ("Cami Junior", cami)):
        for key in ("v3_pct_cumplimiento", "v3_tramo_bruto", "v3_neta"):
            assert row[key] == manual[name][key]
    assert ana["v3_tramo_bruto"] == 100000 and ana["v3_neta"] == 100000


def test_upsert_variables_before_foto_desde_stays_manual(client, db, setup):
    a, _b, _c = setup
    _snapshot(db, [(1, 10.0)], periodo="2026-10")
    r = client.put(
        f"/api/v1/bono/variables/{a.id}?periodo=2026-10", headers=_h(),
        json={"clientes_m2": 0, "clientes_activos": 0, "causas_asignadas": 0, "causas_cumplidas": 0,
              "reclamos_leve": 0, "reclamos_medio": 0, "reclamos_grave": 0, "renovaciones": 0,
              "cumpl_sem1": 95.0},
    )
    assert r.status_code == 200
    assert r.json()["fuente_v3"] == "manual" and r.json()["v3_tramo_bruto"] == 100000
