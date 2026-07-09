"""Integration tests — DecisionEngine wiring into DeadlineEngine.recompute_case.

Verifies recompute_case sets Case.recommended_action_code + Case.next_review_at
(Step 9 of the pipeline), and that the DecisionEngine step preserves the
engine's hard never-raises safe-fail contract even when DecisionEngine itself
blows up. Mirrors the pattern in tests/unit/test_prescripcion.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

# Register all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.core.deadlines_config import DeadlineType, ProceduralState
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.case_litigante import CaseLitigante
from app.models.court import Court
from app.models.lawyer import Lawyer


TODAY = date(2026, 6, 16)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @sa_event.listens_for(eng, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """Pin both engines' clocks to TODAY for deterministic assertions."""
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)
    monkeypatch.setattr("app.services.decision_engine._today_chile", lambda: TODAY)


def _make_case(
    db,
    *,
    competencia: str = "civil",
    filed_at: datetime | None = None,
    last_movement_at: datetime | None = None,
    titulo_tipo: str | None = None,
) -> Case:
    lawyer = Lawyer(
        rut="33333333-3",
        email="decision-wiring@test.com",
        name="Decision Wiring Test Lawyer",
    )
    db.add(lawyer)
    court = Court(name="Juzgado Civil D", code="D001", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="D-0001-2026",
        competencia=competencia,
        filed_at=filed_at or datetime(2024, 1, 1),
        last_movement_at=last_movement_at,
        titulo_tipo=titulo_tipo,
    )
    db.add(case)
    db.flush()
    # Firm defends this case (demandado) — the fixed side for the whole engine.
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante="AB.DDO",
            rut="33333333-3",
            persona_type="NATURAL",
            nombre="Test Abogado",
            natural_key=f"k{case.id}AB.DDO",
        )
    )
    db.flush()
    return case


def _add_movement(db, case_id, movement_date, stage="", description=""):
    from app.models.movement import Movement

    mv = Movement(
        case_id=case_id,
        stage=stage,
        description=description,
        movement_date=datetime.combine(movement_date, datetime.min.time()),
    )
    db.add(mv)
    db.flush()
    return mv


def _recompute(db, case: Case):
    from app.services.deadline_engine import DeadlineEngine

    return DeadlineEngine.recompute_case(db, case)


class TestDecisionEngineWiring:
    def test_recompute_sets_recommended_action_code_for_open_excepciones(self, db) -> None:
        case = _make_case(db)
        notif_date = TODAY - timedelta(days=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.NOTIFICADO.value
        assert case.recommended_action_code == "oponer_excepciones"

    def test_recompute_sets_next_review_at_to_next_deadline(self, db) -> None:
        case = _make_case(db)
        notif_date = TODAY - timedelta(days=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.next_review_at is not None
        assert case.next_review_at == case.next_deadline_at

    def test_recompute_sets_recommended_action_none_when_nothing_pending(self, db) -> None:
        case = _make_case(db)
        _recompute(db, case)
        assert case.recommended_action_code is None
        assert case.next_review_at is None

    def test_recompute_sets_solicitar_abandono_when_abandono_disponible(self, db) -> None:
        case = _make_case(db, last_movement_at=datetime(2024, 1, 1))
        _add_movement(db, case.id, date(2024, 1, 1), "Gestión", "Ordena despachar mandamiento")
        _recompute(db, case)
        assert case.abandono_disponible is True
        assert case.recommended_action_code == "solicitar_abandono"
        assert case.next_review_at is not None

    def test_recompute_never_raises_when_decision_engine_blows_up(self, db, monkeypatch) -> None:
        """A forced DecisionEngine failure must NOT break recompute_case —
        the deadline pipeline (Steps 1-8) must still have run successfully."""
        from app.services.decision_engine import DecisionEngine

        def _boom(*args, **kwargs):
            raise RuntimeError("forced DecisionEngine failure")

        monkeypatch.setattr(DecisionEngine, "recommend", _boom)

        case = _make_case(db)
        notif_date = TODAY - timedelta(days=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")

        # Must not raise.
        _recompute(db, case)

        # The deadline pipeline still succeeded (not GRIS safe-fail).
        assert case.procedural_state == ProceduralState.NOTIFICADO.value
        assert case.semaforo in ("amarillo", "verde", "rojo")
        # The decision step itself safely cleared to None.
        assert case.recommended_action_code is None
        assert case.next_review_at is None

    def test_gris_safe_fail_clears_decision_columns(self, db) -> None:
        """Non-civil competencia → GRIS path — decision columns must also be cleared."""
        case = _make_case(db, competencia="laboral")
        _add_movement(db, case.id, TODAY, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "gris"
        assert case.recommended_action_code is None
        assert case.next_review_at is None
