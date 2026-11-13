"""scripts/reenlazar_sysgal.py core: one-off backfill of unlinked active Sysgal causas."""
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.sysgal_causa import SysgalCausa
from scripts.reenlazar_sysgal import format_report, run


@pytest.fixture
def world(db):
    lw = Lawyer(rut="11111111-1", name="L", role="lawyer")
    court = Court(code="T-RE", name="Juzgado Uno", region="RM", type="civil")
    db.add_all([lw, court])
    db.add(MatrizClasificacion(proc_simple="Juicio Ejecutivo Completo", proc_antiguo="JUICIO EJECUTIVO",
                               etapa="PRUEBA", matriz="M2"))
    db.commit()
    case = Case(lawyer_id=lw.id, court_id=court.id, rol="C-1-2026", status="active", competencia="civil",
                created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(case)
    db.add(SysgalCausa(sysgal_id=1, rol="C-1-2026", rol_norm="C-1-2026", tribunal="Juzgado Uno",
                       estado="activa", eliminada=False, etapa="PRUEBA"))
    db.add(SysgalCausa(sysgal_id=2, rol="C-2-2026", rol_norm="C-2-2026", tribunal="Juzgado Uno",
                       estado="activa", eliminada=False))
    db.commit()
    return case


def test_dry_run_reports_counts_and_writes_nothing(db, world):
    report = run(db, dry_run=True)

    assert (report["candidatas"], report["reenlazadas"], report["sin_calce"], report["reclasificadas"]) == (2, 1, 1, 0)
    db.expire_all()
    assert all(r.case_id is None for r in db.query(SysgalCausa).all())
    assert db.get(Case, world.id).matriz is None
    assert "DRY-RUN" in format_report(report, dry_run=True)


def test_apply_links_and_reclassifies_the_linked_case(db, world):
    report = run(db, dry_run=False)

    db.expire_all()
    assert db.query(SysgalCausa).filter_by(sysgal_id=1).one().case_id == world.id
    assert db.query(SysgalCausa).filter_by(sysgal_id=2).one().case_id is None
    case = db.get(Case, world.id)
    assert (case.matriz, case.matriz_origen) == ("M2", "sysgal_etapa")
    assert (report["reenlazadas"], report["reclasificadas"]) == (1, 1)


def test_report_has_no_sysgal_payload(db, world):
    text = format_report(run(db, dry_run=True), dry_run=True)

    assert "C-1-2026" not in text and "Juzgado" not in text
