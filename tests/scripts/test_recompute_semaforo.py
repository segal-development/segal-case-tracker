"""scripts/recompute_semaforo.py (Fase 0 S6): dry-run reports the changes and writes nothing."""

from datetime import date, datetime

import pytest
from dateutil.relativedelta import relativedelta

from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from scripts.recompute_semaforo import compute_report, format_report

TODAY = date(2026, 11, 16)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


def _mv(db, case, when, stage, desc):
    db.add(Movement(case_id=case.id, stage=stage, procedure="", description=desc,
                    movement_date=datetime.combine(when, datetime.min.time())))


@pytest.fixture
def world(db):
    lawyer = Lawyer(rut="11111111-1", email="a@t.cl", name="Ana", is_active=True)
    admin = Lawyer(rut="22222222-2", email="c@t.cl", name="Carla", role="admin", is_active=True)
    court = Court(name="Juzgado", code="C1", region="RM", type="civil")
    db.add_all([lawyer, admin, court])
    db.flush()

    def case(rol):
        c = Case(lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
                 procedure="Ejecutivo", status="active", filed_at=datetime(2020, 1, 1),
                 last_detail_checked_at=datetime(2026, 11, 1), semaforo="verde")
        db.add(c)
        db.flush()
        db.add(CaseLitigante(case_id=c.id, participante="AB.DDO", rut="11111111-1",
                             persona_type="NATURAL", nombre="Ab", natural_key=f"k{rol}"))
        return c

    ab = case("C-1-2026")  # abandono
    _mv(db, ab, TODAY - relativedelta(years=5), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _mv(db, ab, TODAY - relativedelta(years=4), "Tramitación", "Citación a oír sentencia")
    _mv(db, ab, TODAY - relativedelta(months=42), "Tramitación", "Dicta Sentencia Definitiva")
    ok = case("C-2-2026")  # untouched: sentencia 1y ago
    _mv(db, ok, TODAY - relativedelta(years=2), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _mv(db, ok, TODAY - relativedelta(months=14), "Tramitación", "Citación a oír sentencia")
    _mv(db, ok, TODAY - relativedelta(years=1), "Tramitación", "Dicta Sentencia Definitiva")
    ven = case("C-3-2026")  # fatal vencido
    _mv(db, ven, TODAY - relativedelta(years=2), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _mv(db, ven, TODAY - relativedelta(months=14), "Tramitación", "Citación a oír sentencia")
    _mv(db, ven, TODAY - relativedelta(years=1), "Tramitación", "Dicta Sentencia Definitiva")
    db.add(CaseDeadline(case_id=ven.id, deadline_type="excepciones_8d", due_date=date(2026, 10, 19),
                        triggered_at=date(2026, 10, 1), status="no_cumplido"))
    db.commit()
    return {"ab": ab, "ok": ok, "ven": ven}


def _snapshot(db):
    return (
        sorted((c.id, c.semaforo, c.semaforo_motivo) for c in db.query(Case)),
        db.query(Alert).count(),
    )


def test_dry_run_reports_and_writes_nothing(db, world):
    before = _snapshot(db)
    report = compute_report(db, dry_run=True)
    db.expire_all()
    assert _snapshot(db) == before
    assert report["total"] == 3
    assert report["abandono_verde_a_rojo"] == 1
    assert report["vencido_a_rojo"] == 1
    assert report["alerts_would_create"] == 2  # lawyer + admin for the one lost plazo
    assert report["dry_run"] is True


def test_apply_writes_colors_and_alerts_and_is_idempotent(db, world):
    compute_report(db, dry_run=False)
    db.expire_all()
    by_rol = {c.rol: c for c in db.query(Case)}
    assert by_rol["C-1-2026"].semaforo == "rojo"
    assert by_rol["C-3-2026"].semaforo == "rojo"
    assert by_rol["C-2-2026"].semaforo != "rojo"
    assert db.query(Alert).count() == 2
    again = compute_report(db, dry_run=False)
    assert again["alerts_would_create"] == 0
    assert db.query(Alert).count() == 2


def test_format_report_lines(db, world):
    text = format_report(compute_report(db, dry_run=True))
    assert text.splitlines()[0] == "DRY-RUN (nada se escribió)"
    assert "verde→rojo por abandono: 1" in text
    assert "→rojo por plazo fatal vencido: 1" in text
    assert "alertas de escalamiento a crear: 2" in text


def test_first_emit_after_apply_emits_no_semaforo_rojo(db, world):
    """--apply already moved the cases to rojo: the next sync recompute is not a transition."""
    from app.services.deadline_engine import DeadlineEngine
    from app.services.sync_service import emit_deadline_alerts

    compute_report(db, dry_run=False)
    db.expire_all()
    for case in db.query(Case).filter(Case.semaforo == "rojo").all():
        transition = DeadlineEngine.recompute_case(db, case)
        assert emit_deadline_alerts(db, case, transition) == 0
    assert db.query(Alert).filter(Alert.type == "semaforo_rojo").count() == 0
