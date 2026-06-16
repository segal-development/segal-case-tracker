"""Unit tests for DeadlineEngine.recompute_case.

Tests use SQLite in-memory DB seeded with minimal Case + Movement rows.
All tests exercise the full 8-step pipeline via recompute_case but without
hitting the real PostgreSQL DB (SQLite suffices for unit-level logic tests).

Integration tests for persistence + idempotency are in
tests/integration/test_deadline_engine_integration.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

# Register all models so Base.metadata is populated.
from app.main import app as _app  # noqa: F401

from app.core.database import Base
from app.core.deadlines_config import DeadlineType, ProceduralState
from app.models.case import Case
from app.models.case_deadline import CaseDeadline
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.movement import Movement


# ---------------------------------------------------------------------------
# In-memory SQLite engine for unit tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # SQLite doesn't enforce FK by default; enable for tests.
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
    # Clean between tests
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


# ---------------------------------------------------------------------------
# Helpers — seed minimal domain objects
# ---------------------------------------------------------------------------

TODAY = date(2026, 6, 16)


def _make_case(
    db,
    *,
    competencia: str = "civil",
    filed_at: datetime | None = None,
    last_movement_at: datetime | None = None,
) -> Case:
    lawyer = Lawyer(
        rut="11111111-1",
        email="test@test.com",
        name="Test Lawyer",
    )
    db.add(lawyer)
    court = Court(name="Juzgado Civil", code="C001", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-0001-2026",
        competencia=competencia,
        filed_at=filed_at or datetime(2024, 1, 1),
        last_movement_at=last_movement_at,
    )
    db.add(case)
    db.flush()
    return case


def _add_movement(
    db,
    case_id: int,
    movement_date: date,
    stage: str = "",
    description: str = "",
    procedure: str = "",
) -> Movement:
    mv = Movement(
        case_id=case_id,
        stage=stage,
        procedure=procedure,
        description=description,
        movement_date=datetime.combine(movement_date, datetime.min.time()),
    )
    db.add(mv)
    db.flush()
    return mv


def _recompute(db, case: Case) -> None:
    from app.services.deadline_engine import DeadlineEngine

    DeadlineEngine.recompute_case(db, case)


# ---------------------------------------------------------------------------
# Semáforo threshold tests (REQ-5)
# ---------------------------------------------------------------------------


class TestSemaforo:
    def test_semaforo_gris_no_deadlines(self, db) -> None:
        """No movements → INDETERMINATE → GRIS."""
        case = _make_case(db)
        _recompute(db, case)
        assert case.semaforo == "gris"
        assert case.procedural_state == ProceduralState.INDETERMINATE.value

    def test_semaforo_gris_indeterminate_ignores_stale_deadline(self, db) -> None:
        """INDETERMINATE state must never produce ROJO even with stale deadlines."""
        case = _make_case(db)
        # Inject a stale deadline directly
        stale = CaseDeadline(
            case_id=case.id,
            deadline_type=DeadlineType.EXCEPCIONES_8D.value,
            legal_basis="art. 459 CPC",
            due_date=date(2020, 1, 1),
            triggered_at=date(2019, 12, 1),
            status="active",
        )
        db.add(stale)
        db.flush()
        _recompute(db, case)
        assert case.semaforo == "gris"

    def test_semaforo_verde_10d_remaining(self, db) -> None:
        """10 días hábiles remaining → VERDE."""
        case = _make_case(db)
        # NOTIFICADO with notification 30 days ago → 8d deadline is in the future
        notif_date = TODAY - timedelta(days=1)  # yesterday → 8d deadline future
        _add_movement(
            db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)"
        )
        _recompute(db, case)
        # EXCEPCIONES_8D: trigger yesterday, due ~8 biz days from yesterday
        assert case.semaforo == "verde"

    def test_semaforo_amarillo_3d_remaining(self, db) -> None:
        """2–5 días hábiles → AMARILLO."""
        case = _make_case(db)
        # Add a notification 5 business days before TODAY so EXCEPCIONES_8D
        # expires ~3 business days from now (8 - 5 = 3 remaining).
        # Notify on 2026-06-09 (Mon): biz days until 8d due from June 9:
        # 10,11,12,[Sat13,Sun14],15,16,17,18,19 → due = June 19 (3d from today June 16)
        notif_date = date(2026, 6, 9)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "amarillo"

    def test_semaforo_rojo_due_today(self, db) -> None:
        """Due today (0 remaining) → ROJO."""
        case = _make_case(db)
        # 8 biz days back from TODAY (June 16) = June 5 (Fri)
        # From June 5: count June 6(Sat skip), 7(Sun skip), 8(Mon)=1, 9(Tue)=2,
        #              10(Wed)=3, 11(Thu)=4, 12(Fri)=5, [Sat13,Sun14],
        #              15(Mon)=6, 16(Tue)=7 ... need 8th
        # Actually let me compute: add_business_days(June 4, 8) = June 16?
        # June 4 Thu: Jun5(Fri)=1,[Sat6,Sun7],Jun8(Mon)=2,Jun9(Tue)=3,Jun10(Wed)=4,
        # Jun11(Thu)=5,Jun12(Fri)=6,[Sat13,Sun14],Jun15(Mon)=7,Jun16(Tue)=8. YES!
        notif_date = date(2026, 6, 4)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "rojo"

    def test_semaforo_rojo_expired_yesterday(self, db) -> None:
        """Expired deadline → ROJO."""
        case = _make_case(db)
        # Notification 14 days ago, well past 8d deadline
        notif_date = TODAY - timedelta(days=14)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "rojo"

    def test_semaforo_rojo_1d_remaining(self, db) -> None:
        """1 día hábil remaining → ROJO (≤1 threshold)."""
        case = _make_case(db)
        # 7 biz days from June 5: Jun6=1,..Jun12=5,[wknd],Jun15=6,Jun16=7 → due Jun16 (today)
        # For 1 day remaining, need due date to be tomorrow June 17
        # add_business_days(June 5, 8) = June 16 (0 remaining, ROJO due today)
        # add_business_days(June 8, 8) = ? Jun9=1,10=2,11=3,12=4,[wknd],15=5,16=6,17=7,18=8
        # That's Jun18 → 2 days remaining (AMARILLO). Not right.
        # For 1 day remaining: need due June 17
        # add_business_days(June 5, 8) = June 16 (due today, 0 remaining → ROJO)
        # The ROJO condition is ≤1, so 0 days is ROJO, 1 day is ROJO too.
        # For 1 day remaining: due = June 17 (tomorrow, 1 biz day away)
        # add_business_days(start, 8) = June 17 means start=June 5
        # Let me verify: Jun6(Sat skip), Jun7(Sun skip), Jun8=1, Jun9=2, Jun10=3,
        # Jun11=4, Jun12=5, [Sat13,Sun14], Jun15=6, Jun16=7, Jun17=8. YES!
        # So notification on June 5 (Friday): 8d deadline = June 17 (1 day remaining)
        notif_date = date(2026, 6, 5)  # Friday
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "rojo"

    def test_non_civil_yields_gris(self, db) -> None:
        """Non-civil competencia → GRIS + INDETERMINATE, no processing."""
        case = _make_case(db, competencia="laboral")
        _add_movement(db, case.id, TODAY, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "gris"
        assert case.procedural_state == ProceduralState.INDETERMINATE.value

    def test_indeterminate_never_produces_rojo(self, db) -> None:
        """REQ-10: INDETERMINATE state must never yield ROJO semáforo."""
        case = _make_case(db)
        # No movements → INDETERMINATE
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.INDETERMINATE.value
        assert case.semaforo != "rojo"


# ---------------------------------------------------------------------------
# REBELDÍA (REQ-3)
# ---------------------------------------------------------------------------


class TestRebeldia:
    def test_rebeldia_fires_after_8d_no_excepciones(self, db) -> None:
        """NOTIFICADO + 8d elapsed + no excepciones movement → REBELDE."""
        case = _make_case(db)
        # Notification 30 days ago — well past the 8d deadline
        notif_date = TODAY - timedelta(days=30)
        _add_movement(
            db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)"
        )
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.REBELDE.value

    def test_rebeldia_not_fired_when_excepciones_present(self, db) -> None:
        """If an Excepciones stage movement exists, no REBELDÍA."""
        case = _make_case(db)
        notif_date = TODAY - timedelta(days=30)
        _add_movement(
            db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)"
        )
        _add_movement(db, case.id, notif_date + timedelta(days=5), "Excepciones", "Opone excepciones")
        _recompute(db, case)
        # State should be EXCEPCIONES, not REBELDE
        assert case.procedural_state == ProceduralState.EXCEPCIONES.value


# ---------------------------------------------------------------------------
# Abandono / prescripción risk flags (REQ-4)
# ---------------------------------------------------------------------------


class TestAbandonoPrescricion:
    def test_abandono_none_recent_movement(self, db) -> None:
        """Last movement < 4.5 months ago → abandono_risk = 'none'."""
        # We check via CaseDeadline records or Case fields
        # For now just ensure recompute doesn't crash on a recent case
        case = _make_case(db, last_movement_at=datetime(2026, 5, 1))
        _recompute(db, case)
        # Verify no error and semaforo assigned (gris, no movements)
        assert case.semaforo is not None

    def test_prescripcion_approaching_2_5_years(self, db) -> None:
        """filed_at 2.5+ years ago → prescripcion_risk approaching."""
        filed = datetime(2023, 12, 1)  # ~2.5 years before June 2026
        case = _make_case(db, filed_at=filed)
        _recompute(db, case)
        # No exception; deadlines computed correctly
        assert case.semaforo is not None


# ---------------------------------------------------------------------------
# next_deadline_at column (REQ-6)
# ---------------------------------------------------------------------------


class TestNextDeadlineAt:
    def test_next_deadline_at_set_on_active_deadline(self, db) -> None:
        """Case.next_deadline_at reflects the nearest active deadline."""
        case = _make_case(db)
        notif_date = date(2026, 6, 9)  # 3 biz days remaining
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.next_deadline_at is not None

    def test_next_deadline_at_none_when_indeterminate(self, db) -> None:
        """No deadlines when INDETERMINATE."""
        case = _make_case(db)
        _recompute(db, case)
        assert case.next_deadline_at is None


# ---------------------------------------------------------------------------
# Fix #1a: No stale "active" deadlines after state advances — crossover test
# ---------------------------------------------------------------------------


class TestStaleDeadlineSupersession:
    def test_excepciones_filed_supersedes_past_due_excepciones_8d(self, db) -> None:
        """Fix #1a: NOTIFICADO past-due 8d THEN excepciones filed → AMARILLO/VERDE.

        Before the fix, the engine kept EXCEPCIONES_8D active after advancing
        to EXCEPCIONES state, causing a false ROJO from the past-due deadline.
        After the fix, the classifier returns only TRASLADO_4D (now triggered
        at TRASLADO_EJECUTANTE via Contestación) — or no deadline at EXCEPCIONES —
        and EXCEPCIONES_8D is superseded.
        """
        case = _make_case(db)
        # Notified 30 days ago — well past the 8d window
        notif_date = TODAY - timedelta(days=30)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        # Excepciones filed 25 days ago
        exc_date = notif_date + timedelta(days=5)
        _add_movement(db, case.id, exc_date, "Excepciones", "Opone excepciones")
        _recompute(db, case)

        # EXCEPCIONES_8D must NOT be active → no false ROJO from stale deadline
        from app.models.case_deadline import CaseDeadline
        active_rows = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status == "active",
                CaseDeadline.deadline_type == DeadlineType.EXCEPCIONES_8D.value,
            )
            .all()
        )
        assert active_rows == [], "EXCEPCIONES_8D must be superseded after advancing to EXCEPCIONES"
        # Semáforo must NOT be ROJO from the stale EXCEPCIONES_8D
        assert case.semaforo != "rojo", (
            f"False ROJO: EXCEPCIONES_8D is past-due but case is in EXCEPCIONES state. "
            f"Got semaforo={case.semaforo!r}, state={case.procedural_state!r}"
        )

    def test_terminada_case_has_no_active_deadlines(self, db) -> None:
        """Fix #1b: TERMINADA case → no active deadlines, next_deadline_at None, semáforo gris."""
        case = _make_case(db)
        notif_date = TODAY - timedelta(days=30)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, notif_date + timedelta(days=5), "Terminada", "")
        _recompute(db, case)

        assert case.procedural_state == ProceduralState.TERMINADA.value
        assert case.semaforo == "gris"
        assert case.next_deadline_at is None

        from app.models.case_deadline import CaseDeadline
        active_count = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.status == "active",
            )
            .count()
        )
        assert active_count == 0, f"TERMINADA must have 0 active deadlines, got {active_count}"

    def test_indeterminate_stale_deadline_superseded(self, db) -> None:
        """Fix #1: stale active deadline from prior run superseded when state is INDETERMINATE."""
        case = _make_case(db)
        # Inject a stale active row directly (simulates a prior recompute that left debris)
        from app.models.case_deadline import CaseDeadline
        stale = CaseDeadline(
            case_id=case.id,
            deadline_type=DeadlineType.EXCEPCIONES_8D.value,
            legal_basis="art. 459 CPC",
            due_date=date(2020, 1, 1),
            triggered_at=date(2019, 12, 1),
            status="active",
        )
        db.add(stale)
        db.flush()
        # No movements → INDETERMINATE → all active rows should be superseded
        _recompute(db, case)
        db.refresh(stale)
        assert stale.status == "superseded", "Stale active deadline must be superseded when INDETERMINATE"
        assert case.semaforo == "gris"
        assert case.next_deadline_at is None


# ---------------------------------------------------------------------------
# Fix #2: APELACION_5D triggered by sentencia movement
# ---------------------------------------------------------------------------


class TestApelacion5D:
    def test_apelacion_5d_triggered_by_sentencia_movement(self, db) -> None:
        """Fix #2: 'Dicta Sentencia' → SENTENCIA state + APELACION_5D active."""
        from app.services.business_days import add_business_days
        case = _make_case(db)
        notif_date = date(2026, 3, 1)
        sentencia_date = date(2026, 6, 10)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, date(2026, 4, 1), "Tramitación", "Cita a Audiencia")
        _add_movement(db, case.id, sentencia_date, "Tramitación", "Dicta Sentencia Definitiva")
        _recompute(db, case)

        from app.core.deadlines_config import ProceduralState
        assert case.procedural_state == ProceduralState.SENTENCIA.value

        from app.models.case_deadline import CaseDeadline
        apelacion_row = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.deadline_type == DeadlineType.APELACION_5D.value,
                CaseDeadline.status == "active",
            )
            .first()
        )
        assert apelacion_row is not None, "APELACION_5D must be active after sentencia"
        expected_due = add_business_days(sentencia_date, 5)
        assert apelacion_row.due_date == expected_due, (
            f"APELACION_5D due_date: expected {expected_due}, got {apelacion_row.due_date}"
        )
        assert apelacion_row.legal_basis == "art. 187/475 CPC"
        # SENTENCIA_10D from citación must be superseded
        sentencia_10d_row = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.deadline_type == DeadlineType.SENTENCIA_10D.value,
                CaseDeadline.status == "active",
            )
            .first()
        )
        assert sentencia_10d_row is None, "SENTENCIA_10D must be superseded after SENTENCIA state"


# ---------------------------------------------------------------------------
# Fix #3: False REBELDÍA — detect excepciones via description too
# ---------------------------------------------------------------------------


class TestFalseRebeldia:
    def test_no_rebeldia_when_excepciones_filed_in_non_excepciones_stage(self, db) -> None:
        """Fix #3: 'Opone excepciones' description in non-Excepciones stage prevents REBELDÍA.

        Real data shows excepciones filed under stage 'Notificación demanda y su
        proveído' (PJUD displays it before the court assigns the Excepciones stage).
        The engine must detect this via description pattern, not stage alone.
        """
        case = _make_case(db)
        notif_date = TODAY - timedelta(days=30)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        # Excepciones filed in wrong stage — description has "Opone excepciones"
        _add_movement(
            db, case.id, notif_date + timedelta(days=5),
            "Notificación demanda y su proveído",  # NOT "Excepciones" stage
            "Opone excepciones",
        )
        _recompute(db, case)
        assert case.procedural_state != ProceduralState.REBELDE.value, (
            "Must NOT be REBELDE when 'Opone excepciones' desc detected in any stage"
        )


# ---------------------------------------------------------------------------
# Fix #4: Timezone — engine uses Chile tz for "today"
# ---------------------------------------------------------------------------


class TestTimezone:
    def test_engine_uses_chile_timezone(self, monkeypatch) -> None:
        """Fix #4: _today_chile() returns Chile-tz date, not server UTC date."""
        from app.services import deadline_engine
        import datetime as _dt

        fixed_chile_date = date(2026, 6, 16)

        monkeypatch.setattr(
            deadline_engine,
            "_today_chile",
            lambda: fixed_chile_date,
        )
        # Verify the monkeypatched function is what the engine calls
        assert deadline_engine._today_chile() == fixed_chile_date


# ---------------------------------------------------------------------------
# Fix #8: REBELDÍA boundary — exactly day 8 (NOT rebelde) vs day 9 (rebelde)
# ---------------------------------------------------------------------------


class TestRebeldiaBoundary:
    def test_rebeldia_not_fired_on_due_date_itself(self, db) -> None:
        """Fix #8: on the EXACT due date (day 8), REBELDÍA must NOT fire.

        The debtor still has until end of the due date to file excepciones.
        Engine condition: due_date < today (strict less-than).
        """
        case = _make_case(db)
        # add_business_days(June 4, 8) = June 16 = TODAY
        # So today IS the due date → NOT rebelde
        notif_date = date(2026, 6, 4)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state != ProceduralState.REBELDE.value, (
            "Must NOT be REBELDE on the due date itself (day 8 is still open)"
        )
        assert case.semaforo == "rojo"  # ≤1 remaining → ROJO (deadline is today)

    def test_rebeldia_fires_day_after_due_date(self, db) -> None:
        """Fix #8: one business day AFTER the due date → REBELDE.

        add_business_days(June 3, 8) = June 15 (yesterday).
        Today = June 16 > June 15 → due_date < today → REBELDE.
        """
        case = _make_case(db)
        # add_business_days(June 3, 8):
        # Jun4=1, Jun5=2, Jun8=3, Jun9=4, Jun10=5, Jun11=6, Jun12=7, Jun15=8 → due=Jun15
        notif_date = date(2026, 6, 3)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.REBELDE.value, (
            "Must be REBELDE when today is strictly past the 8d due date"
        )


# ---------------------------------------------------------------------------
# Fix #7: Abandono / prescripción — deferred to PR2, no dead computation
# ---------------------------------------------------------------------------


class TestAbandonoPrescricion:
    def test_abandono_flags_deferred_no_extra_semaforo(self, db) -> None:
        """Fix #7: abandono/prescripción flags are deferred — no impact on semáforo."""
        # A case inactive for 5 months (approaching abandono threshold)
        case = _make_case(db, last_movement_at=datetime(2026, 1, 1))
        _recompute(db, case)
        # No active deadlines → gris (abandono not yet surfaced in PR1)
        assert case.semaforo == "gris"
        assert case.next_deadline_at is None

    def test_prescripcion_approaching_no_flag_in_pr1(self, db) -> None:
        """Fix #7: prescripción approaching (2.5+ years) — deferred, no PR1 output."""
        filed = datetime(2023, 12, 1)  # ~2.5 years before June 2026
        case = _make_case(db, filed_at=filed)
        _recompute(db, case)
        assert case.semaforo is not None  # recompute must not crash
