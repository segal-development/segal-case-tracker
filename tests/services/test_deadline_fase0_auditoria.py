"""Fase 0 de la auditoría: abandono nunca "al día" (S2) y plazo fatal vencido sin escrito (S3/S4).

Everything goes through ``DeadlineEngine.recompute_case`` (the one place the semáforo is computed).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from dateutil.relativedelta import relativedelta

from app.core.deadlines_config import DeadlineType
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine

TODAY = date(2026, 11, 16)
RUT = "11111111-1"
MOTIVO_ABANDONO = "Abandono disponible: presentar el escrito"
ALERT_TYPE = "plazo_fatal_vencido_escalado"


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def lawyer(db):
    obj = Lawyer(rut=RUT, email="a@t.cl", name="Ana Abogada", is_active=True)
    db.add(obj)
    db.commit()
    return obj


@pytest.fixture
def court(db):
    obj = Court(name="12º Juzgado Civil de Santiago", code="C012", region="RM", type="civil")
    db.add(obj)
    db.commit()
    return obj


def _case(db, lawyer, court, rol="C-1-2026", **kw) -> Case:
    c = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        procedure="Ejecutivo", status="active", filed_at=datetime(2020, 1, 1), **kw,
    )
    db.add(c)
    db.flush()
    db.add(CaseLitigante(case_id=c.id, participante="AB.DDO", rut=RUT, persona_type="NATURAL",
                         nombre="Abogado", natural_key=f"ab-{rol}"))
    db.flush()
    return c


def _mv(db, case, when: date, stage: str, desc: str) -> None:
    db.add(Movement(case_id=case.id, stage=stage, procedure="", description=desc,
                    movement_date=datetime.combine(when, datetime.min.time())))
    db.flush()


def _abandono_case(db, lawyer, court, rol="C-1-2026", **kw) -> Case:
    """Post-sentencia, last movement 3.5 years ago: abandono_disponible and nothing red."""
    c = _case(db, lawyer, court, rol, **kw)
    _mv(db, c, TODAY - relativedelta(years=5), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _mv(db, c, TODAY - relativedelta(years=4), "Tramitación", "Citación a oír sentencia")
    _mv(db, c, TODAY - relativedelta(months=42), "Tramitación", "Dicta Sentencia Definitiva")
    return c


def _recent_case(db, lawyer, court, rol="C-2-2026", **kw) -> Case:
    """A fresh, untouched case: no abandono, green."""
    c = _case(db, lawyer, court, rol, **kw)
    _mv(db, c, TODAY - relativedelta(years=2), "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
    _mv(db, c, TODAY - relativedelta(months=14), "Tramitación", "Citación a oír sentencia")
    _mv(db, c, TODAY - relativedelta(years=1), "Tramitación", "Dicta Sentencia Definitiva")
    return c


def _fatal_row(db, case, due: date, *, status="no_cumplido", marked=False, dtype=DeadlineType.EXCEPCIONES_8D):
    row = CaseDeadline(
        case_id=case.id, deadline_type=dtype.value, due_date=due,
        triggered_at=due - timedelta(days=15), status=status,
        marked_by=None, marked_at=datetime(2026, 11, 10) if marked else None,
    )
    db.add(row)
    db.flush()
    return row


class TestS2AbandonoNeverAlDia:
    def test_abandono_is_rojo_with_reason(self, db, lawyer, court):
        case = _abandono_case(db, lawyer, court)
        DeadlineEngine.recompute_case(db, case)
        assert case.abandono_disponible is True
        assert case.semaforo == "rojo"
        assert case.semaforo_motivo == MOTIVO_ABANDONO

    def test_liberacion_override_stays_authoritative(self, db, lawyer, court):
        case = _abandono_case(db, lawyer, court, semaforo_override="verde",
                              semaforo_override_at=datetime(2026, 11, 1), semaforo_override_by="x")
        DeadlineEngine.recompute_case(db, case)
        assert case.abandono_disponible is True
        assert case.semaforo == "verde"
        assert case.semaforo_motivo is None

    def test_terminada_en_sysgal_is_excluded(self, db, lawyer, court):
        case = _abandono_case(db, lawyer, court, terminada_en_sysgal=True)
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo != "rojo"
        assert case.semaforo_motivo is None

    def test_case_without_abandono_keeps_its_color_and_has_no_reason(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        DeadlineEngine.recompute_case(db, case)
        assert case.abandono_disponible is False
        assert case.semaforo != "rojo"
        assert case.semaforo_motivo is None

    def test_reason_is_cleared_when_abandono_goes_away(self, db, lawyer, court):
        case = _abandono_case(db, lawyer, court)
        DeadlineEngine.recompute_case(db, case)
        _mv(db, case, TODAY - relativedelta(days=2), "Tramitación", "Resolución cualquiera")
        DeadlineEngine.recompute_case(db, case)
        assert case.abandono_disponible is False
        assert case.semaforo_motivo is None


class TestS3FatalVencidoSinEscrito:
    def test_recent_unmarked_fatal_no_cumplido_is_rojo_with_reason(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo == "rojo"
        assert case.semaforo_motivo == (
            "Plazo fatal vencido sin escrito: Excepciones (8 días hábiles) venció el 19-10-2026"
        ) or case.semaforo_motivo.startswith("Plazo fatal vencido sin escrito: ")
        assert case.semaforo_motivo.endswith(" venció el 19-10-2026")

    def test_exactly_60_days_still_reddens_61_does_not(self, db, lawyer, court):
        c60 = _recent_case(db, lawyer, court, "C-60-2026")
        _fatal_row(db, c60, TODAY - timedelta(days=60))
        c61 = _recent_case(db, lawyer, court, "C-61-2026")
        _fatal_row(db, c61, TODAY - timedelta(days=61))
        DeadlineEngine.recompute_case(db, c60)
        DeadlineEngine.recompute_case(db, c61)
        assert c60.semaforo == "rojo"
        assert c61.semaforo != "rojo"
        assert c61.semaforo_motivo is None

    def test_marked_by_a_human_stops_reddening(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19), marked=True)
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo != "rojo"
        assert case.semaforo_motivo is None

    def test_non_fatal_no_cumplido_does_not_redden(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19), dtype=DeadlineType.OBSERVACIONES_PRUEBA_6D)
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo != "rojo"

    def test_override_wins_over_fatal_vencido(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court, semaforo_override="verde",
                            semaforo_override_at=datetime(2026, 11, 1), semaforo_override_by="x")
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo == "verde"
        assert case.semaforo_motivo is None

    def test_fatal_vencido_reason_wins_over_abandono(self, db, lawyer, court):
        case = _abandono_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo == "rojo"
        assert case.semaforo_motivo.startswith("Plazo fatal vencido sin escrito: ")


def _admin(db, rut="22222222-2", active=True, role="admin"):
    obj = Lawyer(rut=rut, email=f"{rut}@t.cl", name=f"Admin {rut}", role=role, is_active=active)
    db.add(obj)
    db.flush()
    return obj


class TestS4Escalation:
    def _alerts(self, db):
        return db.query(Alert).filter(Alert.type == ALERT_TYPE).all()

    def test_one_alert_per_deadline_for_lawyer_and_each_active_admin(self, db, lawyer, court):
        admin = _admin(db)
        _admin(db, "33333333-3", active=False)
        _admin(db, "44444444-4", role="lawyer")
        case = _recent_case(db, lawyer, court, rol="C-11467-2026")
        row = _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        alerts = self._alerts(db)
        assert sorted(a.lawyer_id for a in alerts) == sorted([lawyer.id, admin.id])
        for a in alerts:
            assert a.case_id == case.id
            assert a.title == "Plazo fatal vencido sin escrito en C-11467-2026"
            assert "19-10-2026" in a.message and "Ana Abogada" in a.message
            assert a.entity_type == "plazo_fatal_escalado" and a.entity_id == row.id

    def test_recompute_is_idempotent(self, db, lawyer, court):
        _admin(db)
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        first = len(self._alerts(db))
        DeadlineEngine.recompute_case(db, case)
        DeadlineEngine.recompute_case(db, case)
        assert first == 2
        assert len(self._alerts(db)) == 2

    def test_marked_deadline_does_not_escalate(self, db, lawyer, court):
        _admin(db)
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19), marked=True)
        DeadlineEngine.recompute_case(db, case)
        assert self._alerts(db) == []

    def test_old_deadline_does_not_escalate(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, TODAY - timedelta(days=90))
        DeadlineEngine.recompute_case(db, case)
        assert self._alerts(db) == []  # outside the 60-day window: listed, never alerted

    def test_no_alert_without_fatal_no_cumplido(self, db, lawyer, court):
        _admin(db)
        case = _abandono_case(db, lawyer, court)
        DeadlineEngine.recompute_case(db, case)
        assert self._alerts(db) == []

    def test_alert_has_no_email_or_webhook_dispatch(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        (a,) = self._alerts(db)
        assert not a.email_sent and not a.webhook_sent


class TestFollowUpVolumeAndRaces:
    def _alerts(self, db):
        return db.query(Alert).filter(Alert.type == ALERT_TYPE).all()

    def test_gris_case_stays_gris_and_does_not_escalate(self, db, lawyer, court):
        _admin(db)
        case = _case(db, lawyer, court, rol="C-9-2026")  # no movements: indeterminate
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        assert case.semaforo == "gris"
        assert case.semaforo_motivo is None
        assert self._alerts(db) == []

    def test_archived_case_does_not_escalate(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        case.status = "archived"
        _fatal_row(db, case, date(2026, 10, 19))
        DeadlineEngine.recompute_case(db, case)
        assert self._alerts(db) == []

    def test_older_than_60_days_does_not_alert(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, TODAY - timedelta(days=61))
        DeadlineEngine.recompute_case(db, case)
        assert self._alerts(db) == []

    def test_exactly_60_days_alerts_once(self, db, lawyer, court):
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, TODAY - timedelta(days=60))
        DeadlineEngine.recompute_case(db, case)
        assert len(self._alerts(db)) == 1

    def test_unique_index_rejects_a_duplicate_escalation_alert(self, db, lawyer, court):
        from sqlalchemy.exc import IntegrityError

        case = _recent_case(db, lawyer, court)
        row = _fatal_row(db, case, date(2026, 10, 19))

        def mk():
            return Alert(lawyer_id=lawyer.id, case_id=case.id, type=ALERT_TYPE,
                         entity_type="plazo_fatal_escalado", entity_id=row.id, title="t")

        db.add(mk())
        db.flush()
        db.add(mk())
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_race_lost_insert_is_swallowed_and_not_counted(self, db, lawyer, court, monkeypatch):
        """A concurrent worker inserted the same alert after our SELECT: no error, 0 created."""
        from app.services import escalamiento_plazos as esc

        case = _recent_case(db, lawyer, court)
        row = _fatal_row(db, case, date(2026, 10, 19))
        db.add(Alert(lawyer_id=lawyer.id, case_id=case.id, type=ALERT_TYPE,
                     entity_type="plazo_fatal_escalado", entity_id=row.id, title="t"))
        db.flush()
        real_query = db.query

        def blind(*args, **kw):  # the "existing" lookup sees nothing (stale read)
            q = real_query(*args, **kw)
            if len(args) == 2 and args[0] is Alert.entity_id:
                return q.filter(Alert.id < 0)
            return q

        monkeypatch.setattr(db, "query", blind)
        assert esc.escalate_fatal_vencidos(db, case, [row], TODAY) == 0
        monkeypatch.undo()
        assert len(self._alerts(db)) == 1

    def test_one_litigante_resolution_per_case(self, db, lawyer, court, monkeypatch):
        import app.services.lawyer_roster as roster

        calls = []
        real = roster.resolve_case_alert_recipients
        monkeypatch.setattr(roster, "resolve_case_alert_recipients",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        case = _recent_case(db, lawyer, court)
        _fatal_row(db, case, date(2026, 10, 19))
        from app.services import escalamiento_plazos as esc

        esc.escalate_fatal_vencidos(db, case, db.query(CaseDeadline).all(), TODAY)
        assert len(calls) == 1
