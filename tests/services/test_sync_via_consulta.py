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

    detail = _make_detail()
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
