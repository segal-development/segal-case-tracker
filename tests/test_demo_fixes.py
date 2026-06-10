"""
Demo-fix tests — STRICT TDD.
Written BEFORE implementation; all must be RED on first run.

FIX 1 — Notification cap per sync_movements call.
FIX 2 — Lawyer / Webhook queries hoisted out of the movement loop.
FIX 3 — ROL matching normalized (strip + upper) in _select_cases_for_movement_check.
FIX 4 — _resolve_lawyer_id must not 500 on non-numeric sub → 401.
FIX 5 — None-safe caratulado / tribunal in notify_new_movement payload.
"""

import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Side-effect import: registers all models with Base before create_all.
from app.main import app as _app  # noqa: F401

from app.api.deps import get_current_lawyer
from app.core.database import Base
from app.models.alert import Alert
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.models.webhook import Webhook
from app.scrapper.pjud.base import PJUDCase
from app.services.notification_service import NotificationService
from app.services.sync_service import (
    ScrapedMovement,
    SyncService,
    _select_cases_for_movement_check,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_db():
    """Isolated in-memory SQLite session, dropped after each test."""
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
    """Persist Lawyer + Court + Case + one active Webhook in the test DB."""
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

    court = Court(code="TST-001", name="Juzgado Test", region="RM", type="civil")
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


def _movement(folio="99", desc="Citación para oír sentencia", **kwargs):
    defaults = dict(
        folio=folio,
        fecha="10/06/2026",
        tipo_tramite="Resolución",
        descripcion=desc,
        etapa="Término Probatorio",
    )
    defaults.update(kwargs)
    return ScrapedMovement(**defaults)


# ---------------------------------------------------------------------------
# FIX 1 — Notification cap per sync_movements call
# ---------------------------------------------------------------------------


class TestNotificationCap:
    """
    With NOTIFY_MAX_PER_SYNC=2 and 5 new movements:
    - notify_new_movement called exactly 2 times (capped).
    - All 5 Alert rows persisted (alerts are never dropped).
    - A WARNING is logged that mentions the 3 skipped notifications.
    """

    def test_cap_dispatches_only_up_to_limit(
        self, sqlite_db, lawyer_case_webhook, caplog
    ):
        db = sqlite_db
        case = lawyer_case_webhook["case"]

        movements = [_movement(folio=str(i), desc=f"desc-{i}") for i in range(5)]

        with patch("app.services.sync_service.settings") as mock_cfg:
            mock_cfg.NOTIFY_MAX_PER_SYNC = 2

            with patch("app.services.sync_service.NotificationService") as MockNotif:
                mock_instance = MockNotif.return_value

                with caplog.at_level(
                    logging.WARNING, logger="app.services.sync_service"
                ):
                    sync = SyncService(db)
                    new_count, alert_count = sync.sync_movements(case.id, movements)

        # Every movement is new.
        assert new_count == 5
        # All Alert rows must be persisted regardless of cap.
        assert db.query(Alert).count() == 5
        # Notification dispatch is capped at 2.
        assert mock_instance.notify_new_movement.call_count == 2, (
            f"Expected 2 notifications dispatched, got "
            f"{mock_instance.notify_new_movement.call_count}"
        )
        # A warning must be logged mentioning the 3 skipped notifications.
        warning_texts = " ".join(
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert warning_texts, "Expected a warning about the notification cap"
        assert "3" in warning_texts or "skip" in warning_texts.lower(), (
            f"Warning should mention '3' skipped; got: {warning_texts!r}"
        )


# ---------------------------------------------------------------------------
# FIX 2 — Lawyer / Webhook queries hoisted out of the per-movement loop
# ---------------------------------------------------------------------------


class TestQueryHoisting:
    """
    For N new movements the Lawyer and active-Webhook queries must each fire
    exactly ONCE (hoisted before the loop), not once per movement.
    """

    def test_lawyer_and_webhook_queried_once_for_multiple_movements(
        self, sqlite_db, lawyer_case_webhook
    ):
        db = sqlite_db
        case = lawyer_case_webhook["case"]

        # 3 distinct movements so the loop runs 3 times.
        movements = [_movement(folio=str(i), desc=f"desc-{i}") for i in range(3)]

        original_query = db.query
        query_class_calls: list = []

        def tracking_query(*args, **kwargs):
            if args:
                query_class_calls.append(args[0])
            return original_query(*args, **kwargs)

        with patch.object(db, "query", side_effect=tracking_query):
            with patch("app.services.sync_service.NotificationService"):
                sync = SyncService(db)
                sync.sync_movements(case.id, movements)

        lawyer_calls = query_class_calls.count(Lawyer)
        webhook_calls = query_class_calls.count(Webhook)

        assert lawyer_calls == 1, (
            f"Lawyer queried {lawyer_calls} times; expected 1 (hoisted)"
        )
        assert webhook_calls == 1, (
            f"Webhook queried {webhook_calls} times; expected 1 (hoisted)"
        )


# ---------------------------------------------------------------------------
# FIX 3 — ROL matching normalized in _select_cases_for_movement_check
# ---------------------------------------------------------------------------


class TestRolNormalization:
    """
    ROL comparison must use strip().upper() on both sides so that
    'c-123-2026 ' matches a PJUDCase whose .rol is 'C-123-2026'.
    """

    def _cases(self, rols):
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

    def test_lowercase_with_trailing_space_matches_uppercase_stored(self):
        """'c-123-2026 ' must match PJUDCase(rol='C-123-2026')."""
        cases = self._cases(["C-123-2026", "C-456-2026"])
        result = _select_cases_for_movement_check(cases, rol="c-123-2026 ")
        assert len(result) == 1, (
            f"Expected 1 match for normalized rol, got {len(result)}"
        )
        assert result[0].rol == "C-123-2026"

    def test_genuinely_different_rol_returns_empty(self):
        """A rol that has no match after normalization returns []."""
        cases = self._cases(["C-123-2026", "C-456-2026"])
        result = _select_cases_for_movement_check(cases, rol="X-999-2026")
        assert result == [], f"Expected [] for unmatched rol, got {result}"


# ---------------------------------------------------------------------------
# FIX 4 — Non-numeric JWT sub in webhook endpoints must return 401, not 500
# ---------------------------------------------------------------------------


class TestWebhookLawyerIdIntGuard:
    """_resolve_lawyer_id must catch ValueError/TypeError and raise HTTP 401."""

    def test_non_numeric_sub_returns_401_not_500(self, client):
        """POST /webhooks with sub='not-a-number' → 401, not an unhandled 500."""

        async def bad_sub():
            return {"sub": "not-a-number"}

        _app.dependency_overrides[get_current_lawyer] = bad_sub
        try:
            response = client.post(
                "/api/v1/webhooks",
                json={"url": "https://example.com/hook"},
            )
        finally:
            _app.dependency_overrides.pop(get_current_lawyer, None)

        assert response.status_code == 401, (
            f"Expected 401 for non-numeric sub, got {response.status_code}: "
            f"{response.text}"
        )
        assert "Invalid token" in response.json().get("detail", ""), (
            f"Expected 'Invalid token' detail; got {response.json()}"
        )


# ---------------------------------------------------------------------------
# FIX 5 — None-safe caratulado / tribunal in notify_new_movement payload
# ---------------------------------------------------------------------------


class TestNoneSafePayload:
    """
    notify_new_movement with case.plaintiff=None and case.court=None must:
    - not raise AttributeError or TypeError.
    - not produce the literal string 'None' in caratulado.
    - set tribunal to '' (empty string).
    """

    def test_none_plaintiff_and_none_court_produce_safe_payload(self):
        mock_db = MagicMock()
        service = NotificationService(mock_db)

        case = MagicMock()
        case.id = 1
        case.rol = "C-123-2025"
        case.plaintiff = None
        case.defendant = "FERNANDEZ"
        case.court = None

        movement = MagicMock()
        movement.id = 5
        movement.folio = "42"
        movement.movement_date = datetime(2025, 6, 1, 10, 0, 0)
        movement.description = "Test description"
        movement.stage = "Etapa"

        alert = MagicMock()
        alert.id = 1
        lawyer = MagicMock()
        lawyer.id = 99
        lawyer.email = "test@example.cl"

        captured_payload: dict = {}

        def capture_webhook(webhook, payload):
            captured_payload.update(payload)
            return True

        webhook = MagicMock()
        webhook.is_active = True

        with patch.object(service, "send_email_alert", return_value=True):
            with patch.object(service, "send_webhook", side_effect=capture_webhook):
                # Must not raise.
                service.notify_new_movement(alert, case, movement, lawyer, [webhook])

        case_data = captured_payload.get("data", {}).get("case", {})

        assert case_data.get("tribunal") == "", (
            f"Expected empty tribunal when court=None, got {case_data.get('tribunal')!r}"
        )
        caratulado = case_data.get("caratulado", "")
        assert "None" not in caratulado, (
            f"Literal 'None' must not appear in caratulado; got {caratulado!r}"
        )
