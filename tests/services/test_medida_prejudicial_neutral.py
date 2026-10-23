"""A 'Medida prejudicial' row is a neutral, deadline-free row everywhere.

The firm is always the solicitante there and does not run the ejecutivo flow, so
the row carries no semaforo colour, no plazo, no recommendation and raises no
alert. The neutralization is per ROW (``procedure``), never per ROL: a Principal
row of the same ROL keeps the ejecutivo result.
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_lawyer
from app.core.deadlines_config import DeadlineType, ProceduralState
from app.main import app
from app.models.alert import Alert
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.models.redaccion_api_key import RedaccionApiKey
from app.models.sysgal_api_key import SysgalApiKey
from app.services.deadline_engine import DeadlineEngine

TODAY = date(2026, 6, 16)
LAWYER_RUT = "11111111-1"
CLIENT_RUT = "18765432-1"
MEDIDA = "Medida prejudicial"


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


@pytest.fixture
def world(db):
    lawyer = Lawyer(rut=LAWYER_RUT, email="t@t.cl", name="Test Lawyer", is_active=True)
    court = Court(name="Juzgado Civil", code="C001", region="RM", type="civil")
    db.add_all([lawyer, court])
    db.commit()
    return {"lawyer": lawyer, "court": court, "n": 0}


def make_case(db, world, *, procedure, rol="C-1-2026", demandado=True, **kw):
    """A civil case the OLD engine turns rojo/fatal (demandado, notified 12 days ago, 8d term ends today)."""
    world["n"] += 1
    case = Case(
        lawyer_id=world["lawyer"].id, court_id=world["court"].id, rol=rol,
        competencia="civil", procedure=procedure, status="active",
        plaintiff="BANCO X", defendant="PEREZ", filed_at=datetime(2024, 1, 1), **kw,
    )
    db.add(case)
    db.flush()
    db.add(CaseLitigante(
        case_id=case.id, participante="AB.DDO" if demandado else "AB.DTE",
        rut=LAWYER_RUT, persona_type="NATURAL", nombre="Abogado",
        natural_key=f"ab{world['n']}",
    ))
    db.add(CaseLitigante(
        case_id=case.id, participante="DDO.", rut=CLIENT_RUT, persona_type="NATURAL",
        nombre="JUAN PEREZ", natural_key=f"cl{world['n']}",
    ))
    db.add(Movement(
        case_id=case.id, stage="Gestión", procedure="", movement_date=datetime.combine(
            TODAY - timedelta(days=12), datetime.min.time()),
        description="NOTIFICACIÓN DE DEMANDA (Exitosa)",
    ))
    db.commit()
    return case


def add_deadline(db, case, *, status="active", is_manual=False, verdict=None,
                 type_=DeadlineType.EXCEPCIONES_8D.value, triggered=date(2026, 11, 2),
                 due=date(2026, 11, 12)):
    row = CaseDeadline(
        case_id=case.id, deadline_type=type_, due_date=due, triggered_at=triggered,
        status=status, is_manual=is_manual, verdict=verdict,
    )
    db.add(row)
    db.commit()
    return row


def recompute(db, case):
    transition = DeadlineEngine.recompute_case(db, case)
    db.commit()
    return transition


def assert_neutral(case):
    assert case.semaforo == "gris"
    assert case.procedural_state == ProceduralState.INDETERMINATE.value
    assert case.next_deadline_at is None
    assert case.next_deadline_fatal is False
    assert case.abandono_disponible is False
    assert case.prescripcion_cumplida is False
    assert case.plazo_fatal_incumplido is False
    assert case.prescripcion_fecha is None
    assert case.recommended_action_code is None
    assert case.next_review_at is None
    assert case.en_apremio is False
    assert case.semaforo_override is None
    assert case.semaforo_override_at is None
    assert case.semaforo_override_by is None


# ---------------------------------------------------------------------------
# (e) the predicate
# ---------------------------------------------------------------------------


class TestPredicate:
    @pytest.mark.parametrize(
        "value", [MEDIDA, "MEDIDA PREJUDICIAL ", "medida prejudicial", "  Medida   Prejudicial", "Medida prejudicïal"]
    )
    def test_variants_are_medida(self, value) -> None:
        from app.services.deadline_engine import is_medida_prejudicial

        assert is_medida_prejudicial(value) is True

    @pytest.mark.parametrize("value", [None, "", "   ", "Principal", "Ejecutivo", "Medida prejudicial de apremio"])
    def test_everything_else_is_not(self, value) -> None:
        from app.services.deadline_engine import is_medida_prejudicial

        assert is_medida_prejudicial(value) is False

    def test_sql_clause_matches_the_python_predicate(self, db, world) -> None:
        from app.services.deadline_engine import medida_prejudicial_clause

        values = [MEDIDA, "MEDIDA PREJUDICIAL ", "medida prejudicial", None, "", "Principal"]
        for i, v in enumerate(values):
            make_case(db, world, procedure=v, rol=f"C-{i}-2026")
        hits = sorted(
            c.procedure for c in db.query(Case).filter(medida_prejudicial_clause()).all()
        )
        assert hits == sorted([MEDIDA, "MEDIDA PREJUDICIAL ", "medida prejudicial"])
        misses = db.query(Case).filter(~medida_prejudicial_clause()).count()
        assert misses == 3  # NULL, '' and Principal: the negation keeps NULL rows

    @pytest.mark.parametrize("procedure", [None, ""])
    def test_null_and_blank_are_not_neutralized(self, db, world, procedure) -> None:
        case = make_case(db, world, procedure=procedure)
        recompute(db, case)
        assert case.semaforo == "rojo"
        assert case.next_deadline_fatal is True

    @pytest.mark.parametrize("procedure", ["MEDIDA PREJUDICIAL ", "medida prejudicial"])
    def test_variants_are_neutralized_by_the_engine(self, db, world, procedure) -> None:
        case = make_case(db, world, procedure=procedure)
        recompute(db, case)
        assert_neutral(case)


# ---------------------------------------------------------------------------
# (a) (b) (d) the engine
# ---------------------------------------------------------------------------


class TestEngine:
    def test_medida_ends_neutral_even_with_stale_state(self, db, world) -> None:
        case = make_case(
            db, world, procedure=MEDIDA, semaforo="rojo", procedural_state="notificado",
            next_deadline_at=date(2026, 6, 20), next_deadline_fatal=True,
            abandono_disponible=True, prescripcion_cumplida=True, plazo_fatal_incumplido=True,
            prescripcion_fecha=date(2020, 1, 1), recommended_action_code="oponer_excepciones",
            next_review_at=date(2026, 6, 18), en_apremio=True, semaforo_override="verde",
            semaforo_override_at=datetime(2026, 6, 1), semaforo_override_by="x",
        )
        transition = recompute(db, case)
        assert_neutral(case)
        assert transition.new_semaforo == "gris" and transition.new_fatal is False

    def test_old_engine_result_for_the_same_movements_is_rojo_fatal(self, db, world) -> None:
        """Control: without the medida procedure the very same data is rojo/fatal."""
        case = make_case(db, world, procedure="Principal")
        recompute(db, case)
        assert case.semaforo == "rojo"
        assert case.next_deadline_fatal is True
        assert case.recommended_action_code is not None

    def test_stale_deadlines_are_superseded_and_manual_survives(self, db, world) -> None:
        case = make_case(db, world, procedure=MEDIDA)
        expired = add_deadline(db, case, status="expired", verdict="no_cumplido")
        active = add_deadline(
            db, case, status="active", type_=DeadlineType.ACREDITAR_PODER_3D.value,
            triggered=date(2026, 11, 3),
        )
        met = add_deadline(
            db, case, status="met", verdict="cumplido", type_=DeadlineType.APELACION_5D.value,
            triggered=date(2026, 11, 4),
        )
        manual = add_deadline(
            db, case, status="expired", is_manual=True, verdict="no_cumplido",
            type_=DeadlineType.TRASLADO_EJECUTANTE_4D.value, triggered=date(2026, 11, 5),
        )
        recompute(db, case)
        for row in (expired, active, met):
            db.refresh(row)
            assert row.status == "superseded"
            assert row.verdict is None
        db.refresh(manual)
        assert manual.status == "expired" and manual.verdict == "no_cumplido"
        assert db.query(CaseDeadline).filter_by(case_id=case.id).count() == 4  # nothing deleted

    def test_idempotent(self, db, world) -> None:
        case = make_case(db, world, procedure=MEDIDA, semaforo="rojo", next_deadline_fatal=True)
        row = add_deadline(db, case, status="expired", verdict="no_cumplido")
        recompute(db, case)
        recompute(db, case)
        db.refresh(row)
        assert_neutral(case)
        assert (row.status, row.verdict) == ("superseded", None)

    def test_principal_of_the_same_rol_and_court_keeps_the_ejecutivo_result(self, db, world) -> None:
        medida = make_case(db, world, procedure=MEDIDA, rol="C-9-2026")
        principal = make_case(db, world, procedure="Principal", rol="C-9-2026")
        keep = add_deadline(db, principal, status="expired", verdict="no_cumplido")
        gone = add_deadline(db, medida, status="expired", verdict="no_cumplido")
        recompute(db, medida)
        recompute(db, principal)
        assert_neutral(medida)
        assert principal.semaforo == "rojo"
        assert principal.next_deadline_fatal is True
        assert principal.recommended_action_code is not None
        db.refresh(keep)
        db.refresh(gone)
        assert gone.status == "superseded"
        # The Principal row is recomputed by the engine as always: it ends with a live plazo.
        assert (
            db.query(CaseDeadline)
            .filter_by(case_id=principal.id, status="active")
            .count()
            >= 1
        )


# ---------------------------------------------------------------------------
# (c) sync: no alert fan-out
# ---------------------------------------------------------------------------


class TestSyncNoAlerts:
    def test_medida_neutralizes_without_any_alert(self, db, world) -> None:
        from app.services import sync_service

        case = make_case(db, world, procedure=MEDIDA, semaforo="rojo", next_deadline_fatal=True)
        row = add_deadline(db, case, status="expired", verdict="no_cumplido")
        with patch.object(sync_service, "emit_deadline_alerts") as dl, patch.object(
            sync_service, "emit_ratification_alert"
        ) as rat, patch("app.services.notification_service.smtplib.SMTP") as smtp:
            sync_service._maybe_recompute_deadlines(db, case)
        db.commit()
        dl.assert_not_called()
        rat.assert_not_called()
        smtp.assert_not_called()
        assert db.query(Alert).count() == 0
        db.refresh(row)
        assert_neutral(case)
        assert row.status == "superseded"

    def test_principal_still_fans_out(self, db, world) -> None:
        from app.services import sync_service

        case = make_case(db, world, procedure="Principal")
        with patch.object(sync_service, "emit_deadline_alerts") as dl, patch.object(
            sync_service, "emit_ratification_alert"
        ) as rat:
            sync_service._maybe_recompute_deadlines(db, case)
        dl.assert_called_once()
        rat.assert_called_once()
        assert case.semaforo == "rojo"


# ---------------------------------------------------------------------------
# (f) direct readers
# ---------------------------------------------------------------------------


def pair(db, world, **deadline_kw):
    """A medida row and a Principal row of the same ROL, each with the same plazo row."""
    medida = make_case(db, world, procedure=MEDIDA, rol="C-7-2026")
    principal = make_case(db, world, procedure="Principal", rol="C-7-2026")
    return (medida, add_deadline(db, medida, **deadline_kw),
            principal, add_deadline(db, principal, **deadline_kw))


class TestReaders:
    def test_panel_por_vencer(self, db, world) -> None:
        from app.services.semaforo_panel import build_panel

        medida, _, principal, _ = pair(db, world)
        panel = build_panel(db, date(2026, 11, 5))
        assert [r.case_id for r in panel.por_vencer] == [principal.id]

    def test_panel_vencidos(self, db, world) -> None:
        from app.services.semaforo_panel import build_panel

        medida, _, principal, _ = pair(db, world, status="expired", verdict="no_cumplido")
        panel = build_panel(db, date(2026, 11, 20))
        assert [r.case_id for r in panel.vencidos] == [principal.id]

    def test_alert_candidates_and_expired_notice(self, db, world) -> None:
        from app.services.semaforo_alerts import _candidates, expired_unfiled_deadlines

        medida, m_row, principal, p_row = pair(db, world)
        assert [r.id for r in _candidates(db, date(2026, 11, 5))] == [p_row.id]
        m_row.status = p_row.status = "expired"
        m_row.verdict = p_row.verdict = "no_cumplido"
        db.commit()
        assert [r.id for r in expired_unfiled_deadlines(db, date(2026, 11, 20))] == [p_row.id]

    def test_dispatch_expired_skips_the_medida_alert(self, db, world) -> None:
        from app.services.semaforo_alerts import _dispatch_expired

        medida, m_row, principal, p_row = pair(db, world, status="expired", verdict="no_cumplido")
        for case, row in ((medida, m_row), (principal, p_row)):
            db.add(Alert(
                lawyer_id=world["lawyer"].id, case_id=case.id, type="plazo_vencido",
                entity_type="plazo:excepciones_8d:vencido", entity_id=row.id, title="t",
            ))
        db.commit()
        svc = MagicMock()
        assert _dispatch_expired(db, date(2026, 11, 20), svc) == 1
        assert [c.args[0].case_id for c in svc.notify_plazo_alert.call_args_list] == [principal.id]

    def test_sysgal_excepciones_returns_no_plazo_for_medida(self, db, world, client) -> None:
        key = SysgalApiKey(label="t", key_hash=hashlib.sha256(b"k").hexdigest(), is_active=True)
        db.add(key)
        medida, _, principal, _ = pair(db, world, status="expired", verdict="no_cumplido")
        db.commit()
        body = client.get(
            "/api/sysgal/v1/excepciones", params={"cliente_rut": CLIENT_RUT},
            headers={"X-API-Key": "k"},
        ).json()
        assert len(body["causas"]) == 2
        # Same ROL, same caratulado: only the Principal row may carry a plazo.
        assert sum(c["plazo_excepciones"] is not None for c in body["causas"]) == 1

    def test_case_deadlines_endpoint_returns_none_for_medida(self, db, world, client) -> None:
        medida, _, principal, _ = pair(db, world, status="expired", verdict="no_cumplido")
        async def _lawyer():
            return {"sub": str(world["lawyer"].id)}

        client.app.dependency_overrides[get_current_lawyer] = _lawyer
        body_m = client.get(f"/api/v1/cases/{medida.id}/deadlines")
        body_p = client.get(f"/api/v1/cases/{principal.id}/deadlines")
        assert body_m.status_code == 200 and body_p.status_code == 200
        assert body_m.json()["active_deadlines"] == []
        assert body_m.json()["proxima_accion"] is None
        assert len(body_p.json()["active_deadlines"]) == 1

    def test_redaccion_detalle_returns_no_plazos_for_medida(self, db, world, client) -> None:
        db.add(RedaccionApiKey(
            label="t", key_hash=hashlib.sha256(b"rk").hexdigest(), is_active=True))
        medida, _, principal, _ = pair(db, world, status="expired", verdict="no_cumplido")
        db.commit()
        hdr = {"X-API-Key": "rk"}
        m = client.get(f"/api/redaccion/v1/causas/{medida.id}/detalle", headers=hdr)
        p = client.get(f"/api/redaccion/v1/causas/{principal.id}/detalle", headers=hdr)
        assert m.status_code == 200 and p.status_code == 200
        assert m.json()["plazos"]["activos"] == []
        assert len(p.json()["plazos"]["activos"]) == 1


# ---------------------------------------------------------------------------
# (h) query counts
# ---------------------------------------------------------------------------


def _count_statements(db, fn) -> int:
    seen: list[str] = []

    def before(conn, cursor, statement, *a):
        seen.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", before)
    return len(seen)


class TestQueryCount:
    def test_neutral_path_does_not_scale_with_deadlines(self, db, world) -> None:
        few = make_case(db, world, procedure=MEDIDA, rol="C-1-2026")
        many = make_case(db, world, procedure=MEDIDA, rol="C-2-2026")
        add_deadline(db, few)
        for i in range(25):
            add_deadline(
                db, many, triggered=date(2026, 1, 1) + timedelta(days=i),
                due=date(2026, 1, 12) + timedelta(days=i),
            )
        n_few = _count_statements(db, lambda: DeadlineEngine.recompute_case(db, few))
        n_many = _count_statements(db, lambda: DeadlineEngine.recompute_case(db, many))
        assert n_few == n_many
        db.commit()


# ---------------------------------------------------------------------------
# (g) backfill script
# ---------------------------------------------------------------------------


@pytest.fixture
def script(db, monkeypatch):
    mod = importlib.import_module("scripts.neutralize_medida_prejudicial")
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    monkeypatch.setattr(mod, "_open_session", factory)
    return mod


def _snapshot(db):
    db.expire_all()
    cases = [(c.id, c.semaforo, c.next_deadline_fatal, c.procedural_state)
             for c in db.query(Case).order_by(Case.id)]
    rows = [(d.id, d.status, d.verdict) for d in db.query(CaseDeadline).order_by(CaseDeadline.id)]
    return cases, rows


class TestBackfillScript:
    def _seed(self, db, world):
        dirty = make_case(db, world, procedure=MEDIDA, rol="C-1-2026", semaforo="rojo",
                          next_deadline_fatal=True)
        add_deadline(db, dirty, status="expired", verdict="no_cumplido")
        add_deadline(db, dirty, status="active", type_=DeadlineType.ACREDITAR_PODER_3D.value,
                     triggered=date(2026, 11, 3))
        clean = make_case(db, world, procedure=MEDIDA, rol="C-2-2026", semaforo="gris",
                          procedural_state="indeterminate")
        principal = make_case(db, world, procedure="Principal", rol="C-1-2026",
                              semaforo="rojo", next_deadline_fatal=True)
        add_deadline(db, principal, status="expired", verdict="no_cumplido")
        return dirty, clean, principal

    def test_dry_run_writes_nothing_and_reports_counts(self, db, world, script, capsys) -> None:
        self._seed(db, world)
        before = _snapshot(db)
        assert script.main([]) == 0
        out = capsys.readouterr().out
        assert _snapshot(db) == before
        assert "filas medida prejudicial: 2" in out
        assert "no neutras: 1" in out
        assert "plazos a reemplazar: 2" in out
        assert "rojo/amarillo/fatal: 1" in out

    def test_apply_neutralizes_only_medida_rows_and_rerun_is_a_noop(
        self, db, world, script, capsys
    ) -> None:
        dirty, clean, principal = self._seed(db, world)
        assert script.main(["--apply"]) == 0
        db.expire_all()
        assert_neutral(db.get(Case, dirty.id))
        assert {d.status for d in db.query(CaseDeadline).filter_by(case_id=dirty.id)} == {"superseded"}
        assert db.get(Case, principal.id).semaforo == "rojo"
        assert db.query(CaseDeadline).filter_by(case_id=principal.id).one().status == "expired"
        assert db.query(Alert).count() == 0
        after = _snapshot(db)
        capsys.readouterr()
        assert script.main(["--apply"]) == 0
        assert "no neutras: 0" in capsys.readouterr().out
        assert _snapshot(db) == after

    def test_help_exits_zero_without_opening_a_database(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise AssertionError("a database session was opened")

        monkeypatch.setattr("app.core.database.SessionLocal", boom)
        mod = importlib.reload(importlib.import_module("scripts.neutralize_medida_prejudicial"))
        with pytest.raises(SystemExit) as exc:
            mod.main(["--help"])
        assert exc.value.code == 0
        assert "--apply" in capsys.readouterr().out
