"""
Tests for sync pipeline notifications and movement-detection gap.

TDD: tests are written BEFORE implementation — they must fail (RED) first.

Coverage:
1. sync_movements dispatches NotificationService.notify_new_movement for new movements.
2. sync_movements wraps notification in try/except — failure never breaks the sync.
3. Existing movement (not new) → no notification dispatched.
4. _select_cases_for_movement_check: rol provided → only that case returned.
5. _select_cases_for_movement_check: no rol → at most N=5 cases returned.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Force all models to register with Base before create_all
from app.main import app as _app  # noqa: F401 — side-effect import

from app.core.database import Base
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court
from app.models.alert import Alert
from app.models.webhook import Webhook

from app.services.sync_service import (
    SyncService,
    ScrapedMovement,
    _select_cases_for_movement_check,  # added by Task 2 — will be RED until implemented
)
from app.scrapper.pjud.base import PJUDCase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sqlite_db():
    """In-memory SQLite session, isolated per test."""
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


@pytest.fixture()
def lawyer_case_webhook(sqlite_db):
    """Persist Lawyer + Court + Case + active Webhook in the test DB."""
    db = sqlite_db

    lawyer = Lawyer(
        rut="12345678-9",
        name="Abogada Test",
        email="test@segal.cl",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.flush()

    court = Court(
        code="24JCS-TEST",
        name="24 Juzgado Civil de Santiago",
        region="RM",
        type="civil",
    )
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1234-2025",
        plaintiff="BANCO ITAU",
        defendant="FERNANDEZ",
        status="active",
        competencia="civil",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.flush()

    webhook = Webhook(
        lawyer_id=lawyer.id,
        url="https://example.com/webhook",
        events=["movement.created"],
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(webhook)
    db.commit()

    return {"lawyer": lawyer, "case": case, "webhook": webhook}


def _scraped_movement(**kwargs):
    defaults = dict(
        folio="99",
        fecha="10/06/2026",
        tipo_tramite="Resolución",
        descripcion="Citación para oír sentencia",
        etapa="Término Probatorio",
    )
    defaults.update(kwargs)
    return ScrapedMovement(**defaults)


# ---------------------------------------------------------------------------
# Task 1 — sync_movements notification dispatch
# ---------------------------------------------------------------------------

class TestSyncMovementsNotification:
    """sync_movements must dispatch notifications for new movements."""

    def test_new_movement_calls_notify_once(self, sqlite_db, lawyer_case_webhook):
        """
        A new scraped movement → Alert persisted AND
        NotificationService.notify_new_movement called exactly once with
        (alert, case, movement, lawyer, webhooks).
        """
        db = sqlite_db
        case = lawyer_case_webhook["case"]
        webhook = lawyer_case_webhook["webhook"]
        lawyer = lawyer_case_webhook["lawyer"]

        sync = SyncService(db)

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            mock_instance = MockNotif.return_value
            new_count, alert_count = sync.sync_movements(case.id, [_scraped_movement()])

        assert new_count == 1
        assert alert_count == 1

        alert = db.query(Alert).filter(Alert.case_id == case.id).first()
        assert alert is not None

        mock_instance.notify_new_movement.assert_called_once()
        args = mock_instance.notify_new_movement.call_args.args

        assert args[0].id == alert.id          # alert
        assert args[1].id == case.id           # case
        # args[2] is movement (Movement ORM object)
        assert args[3].id == lawyer.id         # lawyer
        assert len(args[4]) == 1               # one active webhook
        assert args[4][0].id == webhook.id

    def test_notification_failure_does_not_break_sync(self, sqlite_db, lawyer_case_webhook):
        """
        If NotificationService.notify_new_movement raises, sync_movements must
        still complete and return the correct (new_count, alert_count).
        Alert must be persisted despite the notification error.
        """
        db = sqlite_db
        case = lawyer_case_webhook["case"]

        sync = SyncService(db)

        with patch("app.services.sync_service.NotificationService") as MockNotif:
            MockNotif.return_value.notify_new_movement.side_effect = RuntimeError(
                "simulated webhook timeout"
            )
            new_count, alert_count = sync.sync_movements(case.id, [_scraped_movement()])

        # Sync did NOT raise
        assert new_count == 1
        assert alert_count == 1
        assert db.query(Alert).count() == 1

    def test_existing_movement_no_notify(self, sqlite_db, lawyer_case_webhook):
        """
        A movement that already exists in the DB is not new → alert_count stays 0
        → NotificationService.notify_new_movement is never called.
        """
        db = sqlite_db
        case = lawyer_case_webhook["case"]
        scraped = [_scraped_movement()]

        sync = SyncService(db)

        # First pass: creates the movement (no mock needed, notification would fire)
        with patch("app.services.sync_service.NotificationService"):
            sync.sync_movements(case.id, scraped)

        # Second pass with identical movement — must be skipped
        with patch("app.services.sync_service.NotificationService") as MockNotif:
            new_count, alert_count = sync.sync_movements(case.id, scraped)

        assert new_count == 0
        assert alert_count == 0
        MockNotif.return_value.notify_new_movement.assert_not_called()


# ---------------------------------------------------------------------------
# Task 2 — _select_cases_for_movement_check scoping helper
# ---------------------------------------------------------------------------

class TestSelectCasesForMovementCheck:
    """Pure-function scoping logic: rol filter vs. N=5 cap."""

    def _pjud_cases(self, rols):
        return [
            PJUDCase(
                rol=rol,
                tribunal="Test Court",
                caratulado="A/B",
                fecha_ingreso="01/01/2025",
                case_token=f"token-{rol}",
            )
            for rol in rols
        ]

    def test_rol_returns_only_matching_case(self):
        cases = self._pjud_cases(["C-1-2025", "C-2-2025", "C-3-2025"])
        result = _select_cases_for_movement_check(cases, rol="C-2-2025")
        assert len(result) == 1
        assert result[0].rol == "C-2-2025"

    def test_rol_not_found_returns_empty(self):
        cases = self._pjud_cases(["C-1-2025", "C-2-2025"])
        result = _select_cases_for_movement_check(cases, rol="C-9-2025")
        assert result == []

    def test_no_rol_caps_at_five(self):
        cases = self._pjud_cases([f"C-{i}-2025" for i in range(10)])
        result = _select_cases_for_movement_check(cases, rol=None)
        assert len(result) == 5

    def test_no_rol_fewer_than_five_returns_all(self):
        cases = self._pjud_cases(["C-1-2025", "C-2-2025"])
        result = _select_cases_for_movement_check(cases, rol=None)
        assert len(result) == 2

    def test_custom_max_cases_is_respected(self):
        cases = self._pjud_cases([f"C-{i}-2025" for i in range(8)])
        result = _select_cases_for_movement_check(cases, rol=None, max_cases=3)
        assert len(result) == 3
