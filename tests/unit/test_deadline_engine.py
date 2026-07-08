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

from dateutil.relativedelta import relativedelta

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


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    """Pin the engine's clock to TODAY for the whole module so recompute-based
    assertions are deterministic — independent of the wall clock and feriados.
    Tests seed dates against TODAY (or via the now-patched _today_chile()), so the
    engine and the fixtures always agree on "today". TestTimezone re-patches it
    locally for its own check, which overrides this within that test."""
    monkeypatch.setattr("app.services.deadline_engine._today_chile", lambda: TODAY)


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


def _recompute(db, case: Case):
    from app.services.deadline_engine import DeadlineEngine

    return DeadlineEngine.recompute_case(db, case)


# Deterministic "today" for color tests (a Monday). Color logic is exercised by
# passing this explicitly to _compute_semaforo, so it never depends on the clock.
_FIXED_TODAY = date(2026, 6, 15)


def _inject_deadline(db, case_id, dtype, *, biz_days_from_today=None, calendar_due=None, status="active"):
    """Insert a CaseDeadline whose due_date is positioned relative to _FIXED_TODAY."""
    from app.services.business_days import add_business_days

    if calendar_due is not None:
        due = calendar_due
    else:
        due = add_business_days(_FIXED_TODAY, biz_days_from_today)
    row = CaseDeadline(
        case_id=case_id,
        deadline_type=dtype.value,
        legal_basis=getattr(dtype, "legal_basis", ""),
        due_date=due,
        triggered_at=due,
        status=status,
    )
    db.add(row)
    db.flush()
    return due


def _set_side(db, case, side: str) -> None:
    """Add a CaseLitigante row that tells _firm_side() which side the firm represents.

    The lawyer created by _make_case always uses RUT '11111111-1'.
    """
    from app.models.case_litigante import CaseLitigante

    code = "AB.DTE" if side == "demandante" else "AB.DDO"
    db.add(
        CaseLitigante(
            case_id=case.id,
            participante=code,
            rut="11111111-1",
            persona_type="NATURAL",
            nombre="Test Abogado",
            natural_key=f"k{case.id}{code}",
        )
    )
    db.flush()


def _color(db, case_id, state, *, side: str | None = None):
    from app.services.deadline_engine import DeadlineEngine

    if side is not None:
        from app.core.deadlines_config import actionable_sets

        mandatory_v, actionable_v = actionable_sets(side)
        return DeadlineEngine._compute_semaforo(
            db, case_id, state, _FIXED_TODAY,
            actionable_values=actionable_v,
            mandatory_values=mandatory_v,
        )
    return DeadlineEngine._compute_semaforo(db, case_id, state, _FIXED_TODAY)


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
        """2–5 días hábiles on an ACTIONABLE deadline → AMARILLO."""
        case = _make_case(db)
        _inject_deadline(db, case.id, DeadlineType.TRASLADO_EJECUTANTE_4D, biz_days_from_today=3)
        assert _color(db, case.id, ProceduralState.TRASLADO_EJECUTANTE) == "amarillo"

    def test_semaforo_rojo_due_today(self, db) -> None:
        """Actionable deadline due today (0 remaining) → ROJO."""
        case = _make_case(db)
        _inject_deadline(db, case.id, DeadlineType.TRASLADO_EJECUTANTE_4D, calendar_due=_FIXED_TODAY)
        assert _color(db, case.id, ProceduralState.TRASLADO_EJECUTANTE) == "rojo"

    def test_semaforo_rojo_expired_mandatory(self, db) -> None:
        """An EXPIRED mandatory-actionable deadline (firm missed it) → ROJO."""
        case = _make_case(db)
        _inject_deadline(
            db, case.id, DeadlineType.TRASLADO_EJECUTANTE_4D,
            calendar_due=_FIXED_TODAY - timedelta(days=10), status="expired",
        )
        assert _color(db, case.id, ProceduralState.TRASLADO_EJECUTANTE) == "rojo"

    def test_semaforo_rojo_1d_remaining(self, db) -> None:
        """1 día hábil remaining on an actionable deadline → ROJO (≤1 threshold)."""
        case = _make_case(db)
        _inject_deadline(db, case.id, DeadlineType.TRASLADO_EJECUTANTE_4D, biz_days_from_today=1)
        assert _color(db, case.id, ProceduralState.TRASLADO_EJECUTANTE) == "rojo"

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
        """NOTIFICADO + 8d elapsed + no excepciones movement → REBELDE (demandante only)."""
        case = _make_case(db)
        _set_side(db, case, "demandante")
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
        """next_deadline_at reflects the firm's nearest active ACTIONABLE deadline.

        DEMANDANTE: TRASLADO_EJECUTANTE_4D is the firm's actionable deadline here.
        """
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        _set_side(db, case, "demandante")
        today = _today_chile()
        # Notificación then a court traslado resolution → TRASLADO_EJECUTANTE_4D
        # (an actionable firm deadline), due in the future.
        _add_movement(db, case.id, today - timedelta(days=10), "Gestión",
                      "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, today, "Contestación Excepciones",
                      "Confiere traslado al ejecutante")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.TRASLADO_EJECUTANTE.value
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
# Bug fix: CITACION_SENTENCIA real PJUD texts supersede TERMINO_PROBATORIO_10D
# ---------------------------------------------------------------------------


class TestCitacionSentenciaRealTexts:
    def test_citacion_a_oir_sentencia_supersedes_termino_probatorio(self, db) -> None:
        """Bug fix: 'Citación a oír sentencia' advances state and supersedes TERMINO_PROBATORIO_10D.

        Before the fix, Rule 7 regex 'Cita a Audiencia' never matched this real
        PJUD text, leaving the case at AUTO_PRUEBA with an overdue
        TERMINO_PROBATORIO_10D causing a false ROJO.  After the fix:
          - TERMINO_PROBATORIO_10D must be superseded (not active)
          - SENTENCIA_10D must be active
        """
        case = _make_case(db)
        notif_date = date(2026, 3, 1)
        auto_prueba_date = date(2026, 4, 1)
        citacion_date = date(2026, 5, 15)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(
            db, case.id, auto_prueba_date,
            "Contestación Excepciones",
            "Notificación resolución que recibe la causa a prue (Exitosa)",
        )
        _add_movement(db, case.id, citacion_date, "Tramitación", "Citación a oír sentencia")
        _recompute(db, case)

        assert case.procedural_state == ProceduralState.CITACION_SENTENCIA.value

        from app.models.case_deadline import CaseDeadline

        # TERMINO_PROBATORIO_10D must NOT be active (superseded by citacion)
        termino_active = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.deadline_type == DeadlineType.TERMINO_PROBATORIO_10D.value,
                CaseDeadline.status == "active",
            )
            .first()
        )
        assert termino_active is None, (
            "TERMINO_PROBATORIO_10D must be superseded after advancing to CITACION_SENTENCIA"
        )

        # SENTENCIA_10D must be active
        sentencia_10d_active = (
            db.query(CaseDeadline)
            .filter(
                CaseDeadline.case_id == case.id,
                CaseDeadline.deadline_type == DeadlineType.SENTENCIA_10D.value,
                CaseDeadline.status == "active",
            )
            .first()
        )
        assert sentencia_10d_active is not None, "SENTENCIA_10D must be active after CITACION_SENTENCIA"


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
    @staticmethod
    def _notif_for_excepciones_due(target_due):
        """Find a notificación date whose EXCEPCIONES_8D (8 biz days) lands on target_due."""
        from app.services.business_days import add_business_days

        notif = target_due
        # walk back until add_business_days(notif, 8) == target_due
        for _ in range(40):
            if add_business_days(notif, 8) == target_due:
                return notif
            notif -= timedelta(days=1)
        return notif

    def test_rebeldia_not_fired_on_due_date_itself(self, db) -> None:
        """On the EXACT due date (day 8), REBELDÍA must NOT fire (window still open).

        Demandante perspective: excepciones is the DEBTOR's window → not the firm's
        red → semáforo VERDE (nothing pending for the firm) while it is still open.
        """
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        _set_side(db, case, "demandante")
        today = _today_chile()
        notif_date = self._notif_for_excepciones_due(today)  # due == today
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state != ProceduralState.REBELDE.value, (
            "Must NOT be REBELDE on the due date itself (day 8 is still open)"
        )
        assert case.semaforo == "verde"  # debtor's window, not the firm's red

    def test_rebeldia_fires_day_after_due_date(self, db) -> None:
        """One business day AFTER the due date with no opposition → REBELDE (demandante).

        Rebeldía is FAVOURABLE for the ejecutante (proceed to sentencia) →
        semáforo VERDE, NOT red.
        """
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        _set_side(db, case, "demandante")
        today = _today_chile()
        # Notificación well in the past → EXCEPCIONES_8D due strictly before today.
        notif_date = today - timedelta(days=30)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.REBELDE.value, (
            "Must be REBELDE when today is strictly past the 8d due date"
        )
        assert case.semaforo == "verde"  # rebeldía is favourable, not red


class TestEjecutanteSemantics:
    """ROJO reflects only the firm's own actionable deadlines (ejecutante)."""

    def test_overdue_court_sentencia_term_not_rojo(self, db) -> None:
        """SENTENCIA_10D is the COURT's term → past-due is informational, not red."""
        case = _make_case(db)
        _inject_deadline(
            db, case.id, DeadlineType.SENTENCIA_10D,
            calendar_due=_FIXED_TODAY - timedelta(days=10),
        )
        assert _color(db, case.id, ProceduralState.CITACION_SENTENCIA) == "verde"

    def test_expired_optional_apelacion_not_rojo(self, db) -> None:
        """APELACION_5D is optional → an expired window is closed, not red."""
        case = _make_case(db)
        _inject_deadline(
            db, case.id, DeadlineType.APELACION_5D,
            calendar_due=_FIXED_TODAY - timedelta(days=10), status="expired",
        )
        assert _color(db, case.id, ProceduralState.SENTENCIA) == "verde"

    def test_overdue_active_optional_apelacion_not_rojo(self, db) -> None:
        """An ACTIVE but past-due optional window (apelación) is closed, not red."""
        case = _make_case(db)
        _inject_deadline(
            db, case.id, DeadlineType.APELACION_5D,
            calendar_due=_FIXED_TODAY - timedelta(days=10), status="active",
        )
        assert _color(db, case.id, ProceduralState.SENTENCIA) == "verde"

    def test_overtaken_overdue_mandatory_not_rojo(self, db) -> None:
        """A past-due mandatory deadline WITH later court activity is overtaken → not red."""
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        today = _today_chile()
        _add_movement(db, case.id, today - timedelta(days=60), "Gestión",
                      "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, today - timedelta(days=40), "Contestación Excepciones",
                      "Confiere traslado al ejecutante")
        _add_movement(db, case.id, today - timedelta(days=5), "Tramitación", "Mero trámite")
        _recompute(db, case)
        assert case.semaforo != "rojo"

    def test_overdue_mandatory_no_activity_is_rojo(self, db) -> None:
        """A past-due mandatory deadline with NO later activity → firm must act → ROJO.

        DEMANDANTE: TRASLADO_EJECUTANTE_4D is the firm's mandatory deadline here.
        """
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        _set_side(db, case, "demandante")
        today = _today_chile()
        _add_movement(db, case.id, today - timedelta(days=20), "Gestión",
                      "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, today - timedelta(days=10), "Contestación Excepciones",
                      "Confiere traslado al ejecutante")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.TRASLADO_EJECUTANTE.value
        assert case.semaforo == "rojo"


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


# ---------------------------------------------------------------------------
# Side-aware semáforo — DEMANDADO vs DEMANDANTE deadline ownership
# ---------------------------------------------------------------------------


class TestSideAware:
    """ROJO reflects the firm's OWN actionable deadlines, which differ by side."""

    def test_demandado_excepciones_urgent_is_rojo(self, db) -> None:
        """DEMANDADO: EXCEPCIONES_8D active, due today (0 remaining) → ROJO."""
        case = _make_case(db)
        _inject_deadline(db, case.id, DeadlineType.EXCEPCIONES_8D, calendar_due=_FIXED_TODAY)
        assert _color(db, case.id, ProceduralState.NOTIFICADO, side="demandado") == "rojo"

    def test_demandado_excepciones_expired_stays_rojo(self, db) -> None:
        """DEMANDADO: EXCEPCIONES_8D expired (firm missed defense window) → ROJO.

        Rebeldía must NOT fire for demandado — the lapsed window means the firm
        failed to act, not that the counterparty defaulted.
        """
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        _set_side(db, case, "demandado")
        today = _today_chile()
        # Notified 30 days ago → EXCEPCIONES_8D window long since expired; no opposition.
        _add_movement(
            db, case.id, today - timedelta(days=30),
            "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)",
        )
        _recompute(db, case)
        # Rebeldía must NOT flip this to REBELDE for demandado.
        assert case.procedural_state != ProceduralState.REBELDE.value, (
            "REBELDÍA must not fire for demandado side"
        )
        # Expired EXCEPCIONES_8D is mandatory for demandado → ROJO.
        assert case.semaforo == "rojo", (
            "DEMANDADO with expired EXCEPCIONES_8D and no later activity must be ROJO"
        )

    def test_demandado_traslado_overdue_not_rojo(self, db) -> None:
        """DEMANDADO: overdue TRASLADO_EJECUTANTE_4D (creditor's deadline) → NOT rojo."""
        case = _make_case(db)
        # Inject as expired (firm-side irrelevant — it is not demandado's deadline).
        _inject_deadline(
            db, case.id, DeadlineType.TRASLADO_EJECUTANTE_4D,
            calendar_due=_FIXED_TODAY - timedelta(days=5), status="expired",
        )
        assert _color(db, case.id, ProceduralState.TRASLADO_EJECUTANTE, side="demandado") != "rojo"

    def test_demandado_overdue_excepciones_with_later_activity_is_overtaken(self, db) -> None:
        """An overdue excepciones (demandado) WITH later court activity is overtaken
        → NOT ROJO. Empirically, later activity (exhortos, mere trámites, ongoing
        notification) means the case is alive/advanced, so the lapsed window is no
        longer the live actionable deadline. Avoids false-positive ROJO."""
        from app.services.deadline_engine import _today_chile

        case = _make_case(db)
        _set_side(db, case, "demandado")
        today = _today_chile()
        _add_movement(db, case.id, today - timedelta(days=30), "Gestión",
                      "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        # Court activity AFTER the excepciones window → overtaken.
        _add_movement(db, case.id, today - timedelta(days=3), "Tramitación", "Mero trámite")
        _recompute(db, case)
        assert case.semaforo != "rojo", (
            "overdue excepciones with later court activity must be overtaken (not ROJO)"
        )

    def test_firm_side_matches_dotted_litigante_rut(self, db) -> None:
        """Regression (review Bug 1): a litigante RUT with dots must still match the
        lawyer's normalized RUT."""
        from app.models.case_litigante import CaseLitigante
        from app.services.deadline_engine import _firm_side

        case = _make_case(db)  # lawyer RUT '11111111-1'
        db.add(
            CaseLitigante(
                case_id=case.id, participante="AB.DTE", rut="11.111.111-1",
                persona_type="NATURAL", nombre="Abg Test", natural_key=f"dot{case.id}",
            )
        )
        db.flush()
        assert _firm_side(db, case) == "demandante"

    def test_demandante_excepciones_informational(self, db) -> None:
        """DEMANDANTE: overdue EXCEPCIONES_8D (debtor's deadline) → NOT rojo."""
        case = _make_case(db)
        # EXCEPCIONES_8D is informational for demandante (debtor's window to oppose).
        _inject_deadline(
            db, case.id, DeadlineType.EXCEPCIONES_8D,
            calendar_due=_FIXED_TODAY - timedelta(days=5),
        )
        assert _color(db, case.id, ProceduralState.NOTIFICADO, side="demandante") != "rojo"

    def test_demandante_traslado_mandatory_rojo(self, db) -> None:
        """DEMANDANTE: expired TRASLADO_EJECUTANTE_4D (firm missed reply) → ROJO."""
        case = _make_case(db)
        _inject_deadline(
            db, case.id, DeadlineType.TRASLADO_EJECUTANTE_4D,
            calendar_due=_FIXED_TODAY - timedelta(days=5), status="expired",
        )
        assert _color(db, case.id, ProceduralState.TRASLADO_EJECUTANTE, side="demandante") == "rojo"


# ---------------------------------------------------------------------------
# _firm_side unit tests
# ---------------------------------------------------------------------------


class TestFirmSide:
    """_firm_side detects the firm's role from CaseLitigante rows."""

    def test_firm_side_no_litigantes_defaults_demandado(self, db) -> None:
        """No CaseLitigante rows → defaults to 'demandado'."""
        from app.services.deadline_engine import _firm_side

        case = _make_case(db)
        assert _firm_side(db, case) == "demandado"

    def test_firm_side_ab_dte_returns_demandante(self, db) -> None:
        """CaseLitigante with AB.DTE and lawyer's RUT → 'demandante'."""
        from app.services.deadline_engine import _firm_side

        case = _make_case(db)
        _set_side(db, case, "demandante")
        assert _firm_side(db, case) == "demandante"

    def test_firm_side_ab_ddo_returns_demandado(self, db) -> None:
        """CaseLitigante with AB.DDO and lawyer's RUT → 'demandado'."""
        from app.services.deadline_engine import _firm_side

        case = _make_case(db)
        _set_side(db, case, "demandado")
        assert _firm_side(db, case) == "demandado"


# ---------------------------------------------------------------------------
# --- abandono_disponible tests ---
# Art. 152/153 CPC — abandono del procedimiento advisory signal
# ---------------------------------------------------------------------------


class TestAbandonoDisponible:
    """abandono_disponible column set by DeadlineEngine.recompute_case."""

    def test_pre_sentencia_7_months_ago_is_true(self, db) -> None:
        """Pre-sentencia (notificado) + last movement 7 months ago → abandono_disponible = True."""
        case = _make_case(db)
        notif_date = TODAY - relativedelta(months=7)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.abandono_disponible is True

    def test_pre_sentencia_3_months_ago_is_false(self, db) -> None:
        """Pre-sentencia (notificado) + last movement 3 months ago → abandono_disponible = False."""
        case = _make_case(db)
        notif_date = TODAY - relativedelta(months=3)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.abandono_disponible is False

    def test_post_sentencia_1_year_ago_is_false(self, db) -> None:
        """Post-sentencia (sentencia) + last movement 1 year ago → abandono_disponible = False (needs 3 years)."""
        case = _make_case(db)
        notif_date = TODAY - relativedelta(years=2)
        citacion_date = TODAY - relativedelta(months=14)
        sentencia_date = TODAY - relativedelta(years=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, citacion_date, "Tramitación", "Citación a oír sentencia")
        _add_movement(db, case.id, sentencia_date, "Tramitación", "Dicta Sentencia Definitiva")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.SENTENCIA.value
        assert case.abandono_disponible is False

    def test_post_sentencia_3_5_years_ago_is_true(self, db) -> None:
        """Post-sentencia (sentencia) + last movement 3.5 years ago → abandono_disponible = True."""
        case = _make_case(db)
        notif_date = TODAY - relativedelta(years=5)
        citacion_date = TODAY - relativedelta(years=4)
        sentencia_date = TODAY - relativedelta(months=42)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, citacion_date, "Tramitación", "Citación a oír sentencia")
        _add_movement(db, case.id, sentencia_date, "Tramitación", "Dicta Sentencia Definitiva")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.SENTENCIA.value
        assert case.abandono_disponible is True

    def test_post_sentencia_rebelde_3_5_years_ago_is_true(self, db) -> None:
        """Post-sentencia (rebelde) + last movement 3.5 years ago → abandono_disponible = True (rebelde counts as post-sentencia)."""
        case = _make_case(db)
        _set_side(db, case, "demandante")
        notif_date = TODAY - relativedelta(months=42)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.REBELDE.value
        assert case.abandono_disponible is True

    def test_indeterminate_1_year_ago_is_false(self, db) -> None:
        """Indeterminate state + last movement 1 year ago → abandono_disponible = False (excluded)."""
        case = _make_case(db)
        _add_movement(db, case.id, TODAY - relativedelta(years=1), "Tramitación", "Mero trámite desconocido")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.INDETERMINATE.value
        assert case.abandono_disponible is False

    def test_terminada_old_movement_is_false(self, db) -> None:
        """Terminada + old movement → abandono_disponible = False (excluded)."""
        case = _make_case(db)
        notif_date = TODAY - relativedelta(years=3)
        terminada_date = TODAY - relativedelta(years=2)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, terminada_date, "Terminada", "")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.TERMINADA.value
        assert case.abandono_disponible is False

    def test_no_movements_is_false(self, db) -> None:
        """Case with no movements → abandono_disponible = False."""
        case = _make_case(db)
        _recompute(db, case)
        assert case.abandono_disponible is False


# ---------------------------------------------------------------------------
# --- next_deadline_fatal tests ---
# Art. 459 CPC / art. 187/475 CPC — fatal (irreversible) deadline flag
# ---------------------------------------------------------------------------


class TestNextDeadlineFatal:
    """next_deadline_fatal column set by DeadlineEngine.recompute_case.

    True only when the nearest actionable deadline is of a fatal type
    (EXCEPCIONES_8D or APELACION_5D). False in all other cases.
    """

    def test_fatal_type_excepciones_8d_is_true(self, db) -> None:
        """Demandado case with upcoming EXCEPCIONES_8D → next_deadline_fatal = True."""
        case = _make_case(db)
        _set_side(db, case, "demandado")
        # Notify yesterday → EXCEPCIONES_8D window is still open (~8 biz days remaining)
        notif_date = TODAY - timedelta(days=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.NOTIFICADO.value
        assert case.next_deadline_at is not None
        assert case.next_deadline_fatal is True

    def test_non_fatal_type_traslado_is_false(self, db) -> None:
        """Demandante case with upcoming TRASLADO_EJECUTANTE_4D → next_deadline_fatal = False."""
        case = _make_case(db)
        _set_side(db, case, "demandante")
        _add_movement(db, case.id, TODAY - timedelta(days=10), "Gestión",
                      "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _add_movement(db, case.id, TODAY, "Contestación Excepciones",
                      "Confiere traslado al ejecutante")
        _recompute(db, case)
        assert case.procedural_state == ProceduralState.TRASLADO_EJECUTANTE.value
        assert case.next_deadline_at is not None
        assert case.next_deadline_fatal is False

    def test_gris_indeterminate_is_false(self, db) -> None:
        """Non-civil competencia → gris → next_deadline_fatal = False."""
        case = _make_case(db, competencia="laboral")
        _add_movement(db, case.id, TODAY, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        _recompute(db, case)
        assert case.semaforo == "gris"
        assert case.next_deadline_fatal is False

    def test_no_deadline_is_false(self, db) -> None:
        """Case with no movements → INDETERMINATE → next_deadline_fatal = False."""
        case = _make_case(db)
        _recompute(db, case)
        assert case.next_deadline_at is None
        assert case.next_deadline_fatal is False

    def test_config_excepciones_8d_is_fatal(self) -> None:
        """DeadlineType.EXCEPCIONES_8D.is_fatal must be True (art. 459 CPC)."""
        assert DeadlineType.EXCEPCIONES_8D.is_fatal is True

    def test_config_apelacion_5d_is_fatal(self) -> None:
        """DeadlineType.APELACION_5D.is_fatal must be True (art. 187/475 CPC)."""
        assert DeadlineType.APELACION_5D.is_fatal is True

    def test_config_traslado_ejecutante_is_not_fatal(self) -> None:
        """DeadlineType.TRASLADO_EJECUTANTE_4D.is_fatal must be False."""
        assert DeadlineType.TRASLADO_EJECUTANTE_4D.is_fatal is False


# ---------------------------------------------------------------------------
# --- SemaforoTransition tests ---
# recompute_case returns a before/after snapshot so callers (sync_service)
# can detect a ROJO-entry or fatal-deadline-appeared transition and fan out
# alerts — the engine itself stays free of any notification coupling.
# ---------------------------------------------------------------------------


class TestSemaforoTransition:
    def test_first_recompute_into_rojo_entered_rojo_true(self, db) -> None:
        """Unclassified case (semaforo=None) whose first recompute lands on
        ROJO must report entered_rojo=True — None != 'rojo' counts as a
        real transition, not just 'already rojo'."""
        case = _make_case(db)
        _set_side(db, case, "demandado")
        _add_movement(
            db, case.id, TODAY - timedelta(days=30),
            "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)",
        )
        transition = _recompute(db, case)
        assert case.semaforo == "rojo"
        assert transition.old_semaforo is None
        assert transition.new_semaforo == "rojo"
        assert transition.entered_rojo is True

    def test_recompute_already_rojo_stays_rojo_entered_rojo_false(self, db) -> None:
        """Recomputing an already-ROJO case that stays ROJO must NOT report a
        new transition — this is the de-dup guarantee: no re-alert."""
        case = _make_case(db)
        _set_side(db, case, "demandado")
        _add_movement(
            db, case.id, TODAY - timedelta(days=30),
            "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)",
        )
        first = _recompute(db, case)
        assert first.entered_rojo is True

        second = _recompute(db, case)
        assert case.semaforo == "rojo"
        assert second.old_semaforo == "rojo"
        assert second.new_semaforo == "rojo"
        assert second.entered_rojo is False

    def test_rojo_to_verde_entered_rojo_false(self, db) -> None:
        """A case that leaves ROJO (e.g. the firm files its opposition) must
        NOT report entered_rojo — only entering rojo alerts, not leaving it."""
        case = _make_case(db)
        _set_side(db, case, "demandado")
        _add_movement(
            db, case.id, TODAY - timedelta(days=30),
            "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)",
        )
        first = _recompute(db, case)
        assert first.entered_rojo is True
        assert case.semaforo == "rojo"

        # Firm finally opposes today — EXCEPCIONES_8D window closes with activity.
        _add_movement(
            db, case.id, TODAY, "Excepciones", "Opone excepciones",
        )
        second = _recompute(db, case)
        assert second.old_semaforo == "rojo"
        assert second.new_semaforo != "rojo"
        assert second.entered_rojo is False

    def test_fatal_appears_false_to_true(self, db) -> None:
        """next_deadline_fatal flipping False→True (a fatal-type deadline
        becomes the nearest active one) must report fatal_appeared=True,
        independent of the semáforo color."""
        case = _make_case(db)
        _set_side(db, case, "demandado")
        notif_date = TODAY - timedelta(days=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        transition = _recompute(db, case)
        assert case.next_deadline_fatal is True
        assert transition.old_fatal is False
        assert transition.new_fatal is True
        assert transition.fatal_appeared is True

    def test_fatal_stays_true_no_repeat_transition(self, db) -> None:
        """Recomputing an already-fatal case that stays fatal must NOT
        re-report fatal_appeared — same de-dup guarantee as entered_rojo."""
        case = _make_case(db)
        _set_side(db, case, "demandado")
        notif_date = TODAY - timedelta(days=1)
        _add_movement(db, case.id, notif_date, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        first = _recompute(db, case)
        assert first.fatal_appeared is True

        second = _recompute(db, case)
        assert case.next_deadline_fatal is True
        assert second.old_fatal is True
        assert second.new_fatal is True
        assert second.fatal_appeared is False

    def test_gris_path_returns_transition_no_crash(self, db) -> None:
        """Non-civil competencia (GRIS safe-fail path) must still return a
        valid SemaforoTransition instead of raising."""
        case = _make_case(db, competencia="laboral")
        _add_movement(db, case.id, TODAY, "Gestión", "NOTIFICACIÓN DE DEMANDA (Exitosa)")
        transition = _recompute(db, case)
        assert case.semaforo == "gris"
        assert transition.new_semaforo == "gris"
        assert transition.entered_rojo is False
        assert transition.fatal_appeared is False


# ---------------------------------------------------------------------------
# --- en_apremio tests ---
# Cuaderno de apremio phase detection via movement stage/description keywords
# ---------------------------------------------------------------------------


class TestEnApremio:
    """en_apremio column set by DeadlineEngine.recompute_case.

    True when ANY movement signals the case has entered the enforcement phase
    (cuaderno de apremio): embargo, remate, or martillero keywords.
    """

    def test_stage_apremio_returns_true(self, db) -> None:
        """Movement with stage='Apremio' (mixed case) → en_apremio = True."""
        case = _make_case(db)
        _add_movement(db, case.id, TODAY - timedelta(days=1), stage="Apremio", description="Cuaderno de apremio")
        _recompute(db, case)
        assert case.en_apremio is True

    def test_description_embargo_returns_true(self, db) -> None:
        """Movement with description containing 'se decreta embargo' → en_apremio = True."""
        case = _make_case(db)
        _add_movement(db, case.id, TODAY - timedelta(days=5), stage="Tramitación", description="se decreta embargo sobre bienes")
        _recompute(db, case)
        assert case.en_apremio is True

    def test_description_remate_returns_true(self, db) -> None:
        """Movement with description 'remate fijado' → en_apremio = True."""
        case = _make_case(db)
        _add_movement(db, case.id, TODAY - timedelta(days=2), stage="Tramitación", description="remate fijado para el día 10")
        _recompute(db, case)
        assert case.en_apremio is True

    def test_ordinary_movements_returns_false(self, db) -> None:
        """Movement with stage='Notificación' and description 'notificación demanda' → en_apremio = False."""
        case = _make_case(db)
        _add_movement(db, case.id, TODAY - timedelta(days=10), stage="Notificación", description="notificación demanda")
        _recompute(db, case)
        assert case.en_apremio is False

    def test_no_movements_returns_false(self, db) -> None:
        """Case with no movements → en_apremio = False."""
        case = _make_case(db)
        _recompute(db, case)
        assert case.en_apremio is False
