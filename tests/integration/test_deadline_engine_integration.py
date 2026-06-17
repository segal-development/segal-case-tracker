"""Integration tests for DeadlineEngine.recompute_case DB persistence.

Uses SQLite in-memory DB (same schema as the unit tests) but focuses on:
  - CaseDeadline rows actually written after recompute_case
  - UNIQUE upsert idempotency: recompute_case twice → no duplicate rows
  - non-civil → semaforo="gris", procedural_state="indeterminate", no rows
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

# Register all models.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.core.deadlines_config import DeadlineType, ProceduralState
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement
from app.services.deadline_engine import DeadlineEngine


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @sa_event.listens_for(eng, "connect")
    def set_pragma(conn, _):
        cur = conn.cursor()
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY_DT = datetime(2026, 6, 16)
TODAY_NOTIF = TODAY_DT - timedelta(days=1)  # yesterday → NOTIFICADO case


def _seed_civil_case_with_notification(db) -> Case:
    lawyer = Lawyer(rut="22222222-2", email="int@test.com", name="Int Lawyer")
    db.add(lawyer)
    court = Court(name="Tribunal Civil", code="TC002", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-INT-0001",
        competencia="civil",
        filed_at=datetime(2025, 1, 1),
    )
    db.add(case)
    db.flush()
    mv = Movement(
        case_id=case.id,
        stage="Gestión",
        description="NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:15/06/2026",
        procedure="Actuación Receptor",
        movement_date=TODAY_NOTIF,
    )
    db.add(mv)
    db.flush()
    return case


@pytest.mark.integration
class TestDeadlineEnginePersistence:
    def test_recompute_creates_case_deadline_rows(self, db) -> None:
        """After recompute_case, at least one CaseDeadline row exists."""
        case = _seed_civil_case_with_notification(db)
        DeadlineEngine.recompute_case(db, case)
        rows = db.query(CaseDeadline).filter(CaseDeadline.case_id == case.id).all()
        assert len(rows) >= 1
        types = {r.deadline_type for r in rows}
        assert DeadlineType.EXCEPCIONES_8D.value in types

    def test_recompute_updates_case_columns(self, db) -> None:
        """After recompute_case, Case.procedural_state and Case.semaforo are set.

        Demandante (ejecutante) perspective: EXCEPCIONES_8D is the debtor's window,
        informational for the firm → VERDE, next_deadline_at None.
        """
        from app.models.case_litigante import CaseLitigante

        case = _seed_civil_case_with_notification(db)
        # Declare the firm as ejecutante (demandante) so EXCEPCIONES_8D stays informational.
        db.add(CaseLitigante(
            case_id=case.id,
            participante="AB.DTE",
            rut="22222222-2",
            persona_type="NATURAL",
            nombre="Int Lawyer",
            natural_key=f"k{case.id}AB.DTE",
        ))
        db.flush()
        DeadlineEngine.recompute_case(db, case)
        assert case.procedural_state == ProceduralState.NOTIFICADO.value
        # NOTIFICADO as demandante: awaiting debtor's excepciones (informational) →
        # no firm-actionable deadline → VERDE, next_deadline_at None.
        assert case.semaforo == "verde"
        assert case.next_deadline_at is None

    def test_recompute_idempotency_no_duplicate_rows(self, db) -> None:
        """Running recompute_case twice must not create duplicate rows."""
        case = _seed_civil_case_with_notification(db)
        DeadlineEngine.recompute_case(db, case)
        db.flush()
        count_after_first = (
            db.query(CaseDeadline).filter(CaseDeadline.case_id == case.id).count()
        )
        DeadlineEngine.recompute_case(db, case)
        db.flush()
        count_after_second = (
            db.query(CaseDeadline).filter(CaseDeadline.case_id == case.id).count()
        )
        assert count_after_second == count_after_first, (
            f"Expected no new rows on second recompute: "
            f"{count_after_first} → {count_after_second}"
        )

    def test_non_civil_case_no_deadline_rows(self, db) -> None:
        """non-civil competencia → semaforo=gris, no CaseDeadline rows."""
        lawyer = Lawyer(rut="33333333-3", email="lab@test.com", name="Lab Lawyer")
        db.add(lawyer)
        court = Court(name="Tribunal Laboral", code="TL003", region="RM", type="laboral")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol="L-INT-0001",
            competencia="laboral",
            filed_at=datetime(2025, 1, 1),
        )
        db.add(case)
        db.flush()
        mv = Movement(
            case_id=case.id,
            stage="Gestión",
            description="NOTIFICACIÓN DE DEMANDA (Exitosa)",
            procedure="Actuación",
            movement_date=TODAY_NOTIF,
        )
        db.add(mv)
        db.flush()

        DeadlineEngine.recompute_case(db, case)

        assert case.semaforo == "gris"
        assert case.procedural_state == ProceduralState.INDETERMINATE.value
        rows = db.query(CaseDeadline).filter(CaseDeadline.case_id == case.id).all()
        assert rows == []
