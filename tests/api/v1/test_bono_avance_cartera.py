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
    # manual weeks that would give 95% for everyone in 2026-09 (no snapshot) and in 2026-10 (snapshot)
    for periodo in ("2026-09", "2026-10"):
        for lw in (a, b, c):
            db.add(BonoVariables(lawyer_id=lw.id, periodo=periodo, nivel=lw.nivel, cumpl_sem1=95.0))
    db.commit()
    return a, b, c


def _snapshot(db, abogados):
    db.add(AvanceCarteraSnapshot(
        periodo="2026-10", desde=datetime(2026, 10, 1).date(), hasta=datetime(2026, 10, 31).date(),
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
    rows = _rows(client, "2026-10")
    ana, beto, cami = rows["Ana Junior"], rows["Beto Pleno"], rows["Cami Junior"]
    assert (ana["fuente_v3"], ana["cobertura_v3"], ana["nota_v3"]) == ("sysgal", 91.5, None)
    assert ana["v3_pct_cumplimiento"] == pytest.approx(0.915) and ana["v3_tramo_bruto"] == 100000
    assert beto["v3_pct_cumplimiento"] == pytest.approx(0.82) and beto["v3_tramo_bruto"] == 64615
    assert cami["v3_tramo_bruto"] == 0 and cami["v3_neta"] == 0
    # manual inputs are still reported untouched
    assert ana["cumpl_total"] == 95.0


def test_mapped_lawyer_absent_from_snapshot_is_zero_with_note(client, db, setup):
    _snapshot(db, [(1, 91.5)])
    cami = _rows(client, "2026-10")["Cami Junior"]
    assert cami["fuente_v3"] == "sysgal" and cami["cobertura_v3"] == 0
    assert cami["nota_v3"] == NOTE
    assert cami["v3_pct_cumplimiento"] == 0 and cami["v3_tramo_bruto"] == 0 and cami["v3_neta"] == 0


def test_month_without_snapshot_stays_manual(client, db, setup):
    _snapshot(db, [(1, 10.0), (2, 10.0), (3, 10.0)])  # snapshot is for 2026-10 only
    ana = _rows(client, "2026-09")["Ana Junior"]
    assert ana["fuente_v3"] == "manual" and ana["cobertura_v3"] is None and ana["nota_v3"] is None
    assert ana["v3_pct_cumplimiento"] == pytest.approx(0.95)
    assert ana["v3_tramo_bruto"] == 100000 and ana["v3_neta"] == 100000


def test_tramo_bands_unchanged_at_edges(client, db, setup):
    _snapshot(db, [(1, 90.0), (2, 79.99), (3, 80.0)])
    rows = _rows(client, "2026-10")
    assert rows["Ana Junior"]["v3_tramo_bruto"] == 100000
    assert rows["Beto Pleno"]["v3_tramo_bruto"] == 0
    assert rows["Cami Junior"]["v3_tramo_bruto"] == 80000


def test_totals_follow_snapshot(client, db, setup):
    _snapshot(db, [(1, 95.0), (2, 95.0), (3, 0.0)])
    body = client.get("/api/v1/bono/liquidacion?periodo=2026-10", headers=_h()).json()
    assert body["totales"]["v3_neta"] == 100000 + 80769


def test_upsert_variables_row_uses_snapshot_too(client, db, setup):
    a, _b, _c = setup
    _snapshot(db, [(1, 85.0)])
    r = client.put(
        f"/api/v1/bono/variables/{a.id}?periodo=2026-10", headers=_h(),
        json={"clientes_m2": 0, "clientes_activos": 0, "causas_asignadas": 0, "causas_cumplidas": 0,
              "reclamos_leve": 0, "reclamos_medio": 0, "reclamos_grave": 0, "renovaciones": 0,
              "cumpl_sem1": 95.0},
    )
    assert r.status_code == 200
    assert r.json()["fuente_v3"] == "sysgal" and r.json()["v3_tramo_bruto"] == 80000
