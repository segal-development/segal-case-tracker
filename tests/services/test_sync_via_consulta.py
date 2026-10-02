"""Tests for sync_via_consulta — PJUD Consulta Unificada persist orchestrator."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.sync_service import SyncService, sync_via_consulta


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def sqlite_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="function")
def seeded(sqlite_db):
    db = sqlite_db
    lawyer = Lawyer(
        rut="11111111-1",
        name="Test Lawyer",
        email="test@example.com",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.flush()

    court = Court(
        code="T-SVC-TEST",
        name="Juzgado Test",
        region="RM",
        type="civil",
    )
    court.pjud_corte = 1  # Santiago corte code
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-9999-2025",
        plaintiff="BANCO TEST",
        defendant="DEUDOR TEST",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    # Refresh so relationships are loaded
    db.refresh(case)
    return {"db": db, "case": case, "lawyer": lawyer, "court": court}


def _make_scraper(detail_or_none):
    """Return a mock scraper whose consulta_by_rol returns detail_or_none."""
    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(return_value=detail_or_none)
    return scraper


def _make_detail(movements=None):
    """Return a minimal mock PJUDCaseDetail."""
    detail = MagicMock()
    detail.movements = movements or []
    detail.litigantes = []
    detail.notificaciones = []
    detail.escritos = []
    detail.exhortos = []
    return detail


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserved_sets_flag_and_skips_timestamp(seeded):
    """consulta_by_rol → None must flag the case as reserved and NOT advance last_detail_checked_at."""
    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    assert case.consulta_reserved is False
    assert case.last_detail_checked_at is None

    scraper = _make_scraper(None)  # Consulta says: not found (reserved)
    pjud_session = MagicMock()

    with patch("app.services.sync_service._maybe_recompute_deadlines"):
        result = await sync_via_consulta(db, lawyer, scraper, pjud_session, [case])

    created_movements, alerts_created, reserved, errors = result

    db.refresh(case)
    assert case.consulta_reserved is True, "reserved flag must be set"
    assert case.last_detail_checked_at is None, "last_detail_checked_at must NOT advance for reserved cases"
    assert reserved == 1
    assert created_movements == 0
    assert errors == 0


@pytest.mark.asyncio
async def test_detail_returned_syncs_and_clears_flag(seeded):
    """consulta_by_rol → PJUDCaseDetail must call sync_movements, advance timestamp, and clear reserved flag."""
    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    # Pre-set the flag so we verify it gets cleared
    case.consulta_reserved = True
    db.commit()

    # Detail WITH movements → the consulta brought the cuaderno → advance the cursor.
    detail = _make_detail(movements=[MagicMock()])
    scraper = _make_scraper(detail)
    pjud_session = MagicMock()

    with (
        patch(
            "app.services.sync_service.convert_api_movements_to_scraped",
            return_value=[MagicMock()],
        ),
        patch.object(SyncService, "sync_movements", return_value=(2, 1)) as mock_sync,
        patch("app.services.sync_service._sync_entities"),
        patch(
            "app.services.sync_service.DocumentPersistenceService"
        ) as mock_dp,
        patch("app.services.sync_service._maybe_recompute_deadlines"),
    ):
        mock_dp.return_value.persist_from_detail.return_value = []
        result = await sync_via_consulta(db, lawyer, scraper, pjud_session, [case])

    created_movements, alerts_created, reserved, errors = result

    db.refresh(case)
    assert case.consulta_reserved is False, "reserved flag must be cleared"
    assert case.last_detail_checked_at is not None, "last_detail_checked_at must be set"
    assert created_movements == 2
    assert alerts_created == 1
    assert reserved == 0
    assert errors == 0
    mock_sync.assert_called_once()


@pytest.mark.asyncio
async def test_zero_movement_consulta_does_not_advance_timestamp(seeded):
    """A consulta that returns a detail with NO movements (cuaderno not public) must
    clear reserved but NOT advance last_detail_checked_at — so the detail rotation
    (Mis Causas) still picks the case up and brings the real movements. Regression
    guard for the 'indeterminate forever' bug."""
    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    assert case.last_detail_checked_at is None

    detail = _make_detail(movements=[])  # case found in consulta, but cuaderno empty/public-hidden
    scraper = _make_scraper(detail)
    pjud_session = MagicMock()

    with (
        patch("app.services.sync_service.convert_api_movements_to_scraped", return_value=[]),
        patch("app.services.sync_service._sync_entities"),
        patch("app.services.sync_service.DocumentPersistenceService") as mock_dp,
        patch("app.services.sync_service._maybe_recompute_deadlines"),
    ):
        mock_dp.return_value.persist_from_detail.return_value = []
        result = await sync_via_consulta(db, lawyer, scraper, pjud_session, [case])

    created_movements, alerts_created, reserved, errors = result
    db.refresh(case)
    assert case.consulta_reserved is False, "case IS in the consulta → not reserved"
    assert case.last_detail_checked_at is None, (
        "0-movement consulta must NOT advance the detail-rotation cursor"
    )
    assert reserved == 0
    assert errors == 0


@pytest.mark.asyncio
async def test_missing_pjud_corte_counts_as_error(sqlite_db):
    """A case whose court has pjud_corte=None must be counted as error without crashing."""
    db = sqlite_db
    lawyer = Lawyer(
        rut="22222222-2",
        name="Lawyer No Corte",
        email="nocorte@example.com",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.flush()

    court = Court(code="T-NOCORTE", name="Sin Corte", region="RM", type="civil")
    # pjud_corte left as None (not set)
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-0001-2025",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    db.refresh(case)

    scraper = _make_scraper(MagicMock())  # would return detail, but should not be called
    pjud_session = MagicMock()

    result = await sync_via_consulta(db, lawyer, scraper, pjud_session, [case])

    created_movements, alerts_created, reserved, errors = result
    assert errors == 1
    assert reserved == 0
    assert created_movements == 0
    scraper.consulta_by_rol.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_no_db_mutations(seeded):
    """dry_run=True must not write to DB regardless of what consulta_by_rol returns."""
    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    original_checked_at = case.last_detail_checked_at  # None
    original_reserved = case.consulta_reserved  # False

    # Test with reserved case first
    scraper_reserved = _make_scraper(None)
    pjud_session = MagicMock()

    result_reserved = await sync_via_consulta(
        db, lawyer, scraper_reserved, pjud_session, [case], dry_run=True
    )
    db.refresh(case)
    assert case.consulta_reserved is False, "dry_run must not set consulta_reserved"
    assert case.last_detail_checked_at is original_checked_at

    # Test with detail returned
    detail = _make_detail()
    scraper_detail = _make_scraper(detail)

    with (
        patch(
            "app.services.sync_service.convert_api_movements_to_scraped",
            return_value=[MagicMock()],
        ),
        patch.object(SyncService, "sync_movements", return_value=(2, 1)) as mock_sync,
        patch("app.services.sync_service._maybe_recompute_deadlines"),
    ):
        result_detail = await sync_via_consulta(
            db, lawyer, scraper_detail, pjud_session, [case], dry_run=True
        )

    db.refresh(case)
    assert case.consulta_reserved is False, "dry_run must not clear consulta_reserved"
    assert case.last_detail_checked_at is None, "dry_run must not advance last_detail_checked_at"
    mock_sync.assert_not_called()


@pytest.mark.asyncio
async def test_consulta_session_expired_reauth_succeeds(seeded):
    """ConsultaSessionExpired → reauth_callback returns a new session → retry succeeds → errors=0, reserved=0."""
    from app.scrapper.pjud.exceptions import ConsultaSessionExpired

    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    detail = _make_detail()
    fresh_session = MagicMock()

    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(
        side_effect=[ConsultaSessionExpired("session expired"), detail]
    )
    reauth_callback = AsyncMock(return_value=fresh_session)
    pjud_session = MagicMock()

    with (
        patch("app.services.sync_service.convert_api_movements_to_scraped", return_value=[]),
        patch("app.services.sync_service._sync_entities"),
        patch("app.services.sync_service.DocumentPersistenceService") as mock_dp,
        patch("app.services.sync_service._maybe_recompute_deadlines"),
    ):
        mock_dp.return_value.persist_from_detail.return_value = []
        result = await sync_via_consulta(
            db, lawyer, scraper, pjud_session, [case], reauth_callback=reauth_callback
        )

    created_movements, alerts_created, reserved, errors = result

    assert errors == 0
    assert reserved == 0
    reauth_callback.assert_awaited_once()
    assert scraper.consulta_by_rol.await_count == 2
    db.refresh(case)
    assert case.consulta_reserved is False


@pytest.mark.asyncio
async def test_consulta_session_expired_no_reauth_counts_as_error(seeded):
    """ConsultaSessionExpired with no reauth_callback → errors=1, reserved=0, consulta_reserved unchanged."""
    from app.scrapper.pjud.exceptions import ConsultaSessionExpired

    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(side_effect=ConsultaSessionExpired("session expired"))
    pjud_session = MagicMock()

    result = await sync_via_consulta(db, lawyer, scraper, pjud_session, [case])

    created_movements, alerts_created, reserved, errors = result
    assert errors == 1
    assert reserved == 0
    db.refresh(case)
    assert case.consulta_reserved is False, "session expiry must NOT set consulta_reserved"


@pytest.mark.asyncio
async def test_consulta_session_expired_reauth_returns_none_counts_as_error(seeded):
    """ConsultaSessionExpired + reauth_callback returning None → errors=1, reserved=0."""
    from app.scrapper.pjud.exceptions import ConsultaSessionExpired

    db = seeded["db"]
    case = seeded["case"]
    lawyer = seeded["lawyer"]

    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(side_effect=ConsultaSessionExpired("session expired"))
    reauth_callback = AsyncMock(return_value=None)
    pjud_session = MagicMock()

    result = await sync_via_consulta(
        db, lawyer, scraper, pjud_session, [case], reauth_callback=reauth_callback
    )

    created_movements, alerts_created, reserved, errors = result
    assert errors == 1
    assert reserved == 0
    db.refresh(case)
    assert case.consulta_reserved is False, "session expiry must NOT set consulta_reserved"


# ---------------------------------------------------------------------------
# Observability: the ORIGINAL error must reach the log
# ---------------------------------------------------------------------------


class _PoisonableCase:
    """Case-like object whose ORM attributes raise once the session is poisoned.

    Mimics an expired attribute on a session that needs a rollback: reading
    ``id`` raises something that is NOT an AttributeError, so ``getattr(obj,
    "id", "?")`` does not protect the log call.
    """

    rol = "C-4242-2025"
    court_id = 1
    consulta_reserved = False

    def __init__(self, court):
        self.court = court
        self.poisoned = False

    @property
    def id(self):
        if self.poisoned:
            from sqlalchemy.exc import PendingRollbackError

            raise PendingRollbackError(
                "This Session's transaction has been rolled back due to a "
                "previous exception during flush."
            )
        return 42


def _proxy_cut_error():
    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "INSERT ...", {}, Exception("server closed the connection unexpectedly")
    )


def _logged_text(caplog) -> str:
    import traceback

    parts = []
    for r in caplog.records:
        parts.append(r.getMessage())
        if r.exc_info:
            parts.append("".join(traceback.format_exception(*r.exc_info)))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_failure_log_survives_case_id_read_raising_non_attribute_error(
    seeded, caplog
):
    """Reading case.id raises PendingRollbackError after the failure; the log
    call must not depend on it (getattr's default only covers AttributeError)."""
    db = seeded["db"]
    case = _PoisonableCase(seeded["court"])

    async def _fail(*args, **kwargs):
        case.poisoned = True
        raise _proxy_cut_error()

    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(side_effect=_fail)

    with caplog.at_level("DEBUG", logger="app.services.sync_service"):
        result = await sync_via_consulta(
            db, seeded["lawyer"], scraper, MagicMock(), [case]
        )

    assert result[3] == 1  # counted as an error, batch not aborted
    text = _logged_text(caplog)
    assert "server closed the connection unexpectedly" in text
    assert "C-4242-2025" in text


@pytest.mark.asyncio
async def test_original_error_logged_even_if_rollback_explodes(
    seeded, caplog, monkeypatch
):
    db = seeded["db"]
    case = seeded["case"]

    def _broken_rollback():
        raise RuntimeError("connection is gone")

    async def _fail(*args, **kwargs):
        monkeypatch.setattr(db, "rollback", _broken_rollback)
        raise _proxy_cut_error()

    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(side_effect=_fail)

    with caplog.at_level("DEBUG", logger="app.services.sync_service"):
        with pytest.raises(RuntimeError, match="connection is gone"):
            await sync_via_consulta(
                db, seeded["lawyer"], scraper, MagicMock(), [case]
            )

    assert "server closed the connection unexpectedly" in _logged_text(caplog)


@pytest.mark.asyncio
async def test_dead_connection_at_loop_start_fails_one_case_not_the_batch(
    seeded, caplog
):
    """A case whose identity cannot be read must count as ONE failure.

    The identity capture has to live INSIDE the try. Hoisted above it, a
    connection already dead from a previous iteration makes that read raise
    outside the handler and the whole lawyer's batch aborts, losing every
    remaining case instead of just the broken one.
    """
    db = seeded["db"]
    muerta = _PoisonableCase(seeded["court"])
    muerta.poisoned = True  # identity unreadable before the body even runs
    sana = seeded["case"]

    detail = MagicMock()
    detail.movements = []
    scraper = MagicMock()
    scraper.consulta_by_rol = AsyncMock(return_value=detail)

    with caplog.at_level("DEBUG", logger="app.services.sync_service"):
        result = await sync_via_consulta(
            db, seeded["lawyer"], scraper, MagicMock(), [muerta, sana]
        )

    assert result[3] == 1, "the unreadable case must count as exactly one error"
    # The healthy case that came after it was still processed.
    assert scraper.consulta_by_rol.await_count == 1
    assert "PendingRollbackError" in _logged_text(caplog)
