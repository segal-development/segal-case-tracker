"""scripts/reporte_duenio_sysgal.py: read-only per-lawyer gana/pierde report (S3)."""
from datetime import datetime

import pytest

from app.models.case import Case
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.goal import Goal
from app.models.lawyer import Lawyer
from app.models.sysgal_causa import SysgalCausa
from app.models.sysgal_usuario import SysgalUsuario
from scripts.reporte_duenio_sysgal import format_report, run


@pytest.fixture
def world(db):
    court = Court(code="T-RP", name="Juzgado RP", region="RM", type="civil")
    pjud = Lawyer(rut="11111111-1", name="Pjud", role="lawyer", is_active=True)
    edu = Lawyer(rut="22222222-2", name="Eduardo", role="lawyer", is_active=True)
    db.add_all([court, pjud, edu])
    db.flush()
    db.add(SysgalUsuario(sysgal_id=10, nombre="e", lawyer_id=edu.id))
    case = Case(lawyer_id=pjud.id, court_id=court.id, rol="C-1-2026", competencia="civil", status="active")
    db.add(case)
    db.flush()
    db.add(CaseLitigante(case_id=case.id, participante="AB.DTE", rut=pjud.rut, nombre="Pjud",
                         persona_type="NATURAL", natural_key="k"))
    db.add(SysgalCausa(sysgal_id=1, rol="C-1-2026", rol_norm="C-1-2026", tribunal="J", estado="activa",
                       eliminada=False, abogado_sysgal_id=10, sysgal_updated_at=datetime(2026, 11, 1),
                       case_id=case.id))
    db.commit()


def test_run_returns_counts_and_does_not_turn_the_switch_on(db, world):
    assert run(db) == {"Eduardo": {"gana": 1, "pierde": 0}, "Pjud": {"gana": 0, "pierde": 1}}
    assert db.query(Goal).count() == 0


def test_format_report_lists_each_lawyer_sorted_by_gain_and_totals(db, world):
    text = format_report(run(db))
    lines = text.splitlines()
    assert lines[0] == "Cambio de dueño si se activa sysgal_es_fuente_duenio (el switch NO se toca)"
    assert lines[1].split() == ["abogado", "gana", "pierde", "neto"]
    assert lines[2].split() == ["Eduardo", "1", "0", "+1"]
    assert lines[3].split() == ["Pjud", "0", "1", "-1"]
    assert lines[-1].split() == ["TOTAL", "1", "1", "+0"]


def test_format_report_when_nothing_changes():
    assert format_report({}) == (
        "Cambio de dueño si se activa sysgal_es_fuente_duenio (el switch NO se toca)\n"
        "Sin cambios: ningún caso cambiaría de dueño."
    )
