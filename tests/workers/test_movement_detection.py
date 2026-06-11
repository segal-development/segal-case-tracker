"""S4-T2, S4-T4: Tests for worker movement detection (Slice 4).

S4-T2: detect_and_sync_movements selects cases for movement check (scoped, not all
       cases), fetches detail via mocked scraper, and persists new Movement rows
       + sets last_movement_at on the Case.

S4-T4: A new movement triggers NotificationService.notify_new_movement dispatch.

All tests mock the scraper, Redis, and NotificationService — no live connections.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api_case(rol: str, case_token: str = "token-123"):
    """Create a minimal mock PJUDCase-like object."""
    m = MagicMock()
    m.rol = rol
    m.case_token = case_token
    return m


def _make_detail_with_movements(movements_data: list):
    """Build a mock scraper detail response with the given movement dicts.

    Includes all PJUDCaseDetail fields consumed by DocumentPersistenceService
    so that wiring the service into detect_and_sync_movements does not cause
    unexpected MagicMock-truthy attribute access failures.
    """
    detail = MagicMock()
    # Case-level documents (texto_demanda, cert_envio, ebook) — none in unit tests.
    detail.case_documents = []
    mocks = []
    for d in movements_data:
        mv = MagicMock()
        mv.folio = d.get("folio", "001")
        mv.fecha = d.get("fecha", "01/06/2024")
        mv.tipo_tramite = d.get("tipo_tramite", "Resolución")
        mv.descripcion = d.get("descripcion", "Test movement")
        mv.etapa = d.get("etapa", "Cuaderno Principal")
        mv.foja = d.get("foja", "1")
        mv.tiene_documento = d.get("tiene_documento", False)
        # Document token fields — None/empty so persist_from_detail is a no-op.
        mv.documento_token = None
        mv.documentos = []
        mocks.append(mv)
    detail.movements = mocks
    return detail


def _setup_lawyer_case(db, *, rol: str) -> tuple:
    """Create a Lawyer + Court + Case row and return (lawyer, case)."""
    lawyer = Lawyer(rut="12345678-9", name="Test Lawyer", is_active=True)
    db.add(lawyer)
    db.flush()

    court = Court(code="TEST-COURT", name="Tribunal Civil Test", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        competencia="civil",
        plaintiff="Alice",
        defendant="Bob",
        status="active",
    )
    db.add(case)
    db.commit()
    return lawyer, case


# ===========================================================================
# S4-T2: detect_and_sync_movements behaviour
# ===========================================================================

class TestDetectAndSyncMovements:
    """detect_and_sync_movements correctly scopes, fetches, and persists movements."""

    @pytest.mark.asyncio
    async def test_selects_cases_using_movement_check_scoping(self, db):
        """get_case_detail is called at most MOVEMENT_CHECK_DEFAULT_MAX times — not for every case."""
        from app.services.sync_service import (
            detect_and_sync_movements,
            MOVEMENT_CHECK_DEFAULT_MAX,
        )

        # Create more cases than the cap so the scoping must kick in.
        total_cases = MOVEMENT_CHECK_DEFAULT_MAX + 3
        api_cases = [
            _make_api_case(f"C-{i}-2024", f"token-{i}") for i in range(total_cases)
        ]

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            return_value=_make_detail_with_movements([])
        )

        _, _, _ = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=1,
            api_cases=api_cases,
        )

        assert mock_scraper.get_case_detail.await_count == MOVEMENT_CHECK_DEFAULT_MAX

    @pytest.mark.asyncio
    async def test_new_movements_persisted_and_last_movement_at_set(self, db):
        """New movements from get_case_detail are written to DB and case.last_movement_at is set."""
        from app.services.sync_service import detect_and_sync_movements

        lawyer, case = _setup_lawyer_case(db, rol="C-100-2024")

        api_cases = [_make_api_case("C-100-2024", "token-abc")]
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            return_value=_make_detail_with_movements([
                {
                    "folio": "001",
                    "fecha": "01/06/2024",
                    "tipo_tramite": "Resolución",
                    "descripcion": "Auto: se tiene presente",
                }
            ])
        )

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=api_cases,
        )

        assert movements_new == 1
        assert alerts_created == 1
        assert not errors

        db.refresh(case)
        assert case.last_movement_at is not None

    @pytest.mark.asyncio
    async def test_cases_without_case_token_are_skipped(self, db):
        """Cases whose case_token is falsy are silently skipped — no get_case_detail call."""
        from app.services.sync_service import detect_and_sync_movements

        no_token = _make_api_case("C-200-2024", case_token=None)
        no_token.case_token = None

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock()

        movements_new, _, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=1,
            api_cases=[no_token],
        )

        mock_scraper.get_case_detail.assert_not_awaited()
        assert movements_new == 0
        assert not errors

    @pytest.mark.asyncio
    async def test_get_case_detail_failure_is_recorded_not_raised(self, db):
        """If get_case_detail raises, the error is captured in the errors list; no exception propagates."""
        from app.services.sync_service import detect_and_sync_movements

        api_cases = [_make_api_case("C-300-2024", "token-fail")]
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=Exception("PJUD timeout"))

        movements_new, _, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=1,
            api_cases=api_cases,
        )

        assert movements_new == 0
        assert len(errors) == 1
        assert "C-300-2024" in errors[0]

    @pytest.mark.asyncio
    async def test_rol_filter_restricts_to_single_case(self, db):
        """When rol is provided, only that case is detail-fetched."""
        from app.services.sync_service import detect_and_sync_movements

        api_cases = [
            _make_api_case("C-400-2024", "token-400"),
            _make_api_case("C-401-2024", "token-401"),
        ]
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            return_value=_make_detail_with_movements([])
        )

        await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=1,
            api_cases=api_cases,
            rol="C-400-2024",
        )

        assert mock_scraper.get_case_detail.await_count == 1


# ===========================================================================
# S4-T3: Worker wiring
# ===========================================================================

class TestWorkerMovementWiring:
    """sync_lawyer_cases calls detect_and_sync_movements and surfaces movements_new."""

    @pytest.mark.asyncio
    async def test_sync_lawyer_cases_calls_movement_detection_after_case_sync(self):
        """After sync_cases completes, detect_and_sync_movements is awaited."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()
        mock_pjud_session = MagicMock()

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session_by_lawyer = AsyncMock(return_value=mock_pjud_session)
            mock_store_fn.return_value = mock_store

            with patch("app.api.v1.pjud.get_scraper") as mock_get_scraper:
                fake_case = _make_api_case("C-500-2024", "token-500")
                mock_scraper = MagicMock()
                mock_scraper.get_my_cases = AsyncMock(return_value=[fake_case])
                mock_scraper.close = AsyncMock()
                mock_get_scraper.return_value = mock_scraper

                with patch(
                    "app.workers.sync_scheduler.detect_and_sync_movements",
                    new_callable=AsyncMock,
                ) as mock_detect:
                    mock_detect.return_value = (0, 0, [])

                    with patch("app.workers.sync_scheduler.SyncService") as mock_svc_cls:
                        mock_svc = MagicMock()
                        mock_result = MagicMock()
                        mock_result.cases_total = 1
                        mock_result.cases_new = 0
                        mock_svc.sync_cases.return_value = mock_result
                        mock_svc_cls.return_value = mock_svc

                        result = await sync_lawyer_cases(
                            lawyer_id=1, competencia="civil", db=mock_db
                        )

        mock_detect.assert_awaited_once()
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_sync_lawyer_cases_returns_movements_new_in_result(self):
        """movements_new from detect_and_sync_movements is included in the return dict."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()
        mock_pjud_session = MagicMock()

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session_by_lawyer = AsyncMock(return_value=mock_pjud_session)
            mock_store_fn.return_value = mock_store

            with patch("app.api.v1.pjud.get_scraper") as mock_get_scraper:
                fake_case = _make_api_case("C-600-2024", "token-600")
                mock_scraper = MagicMock()
                mock_scraper.get_my_cases = AsyncMock(return_value=[fake_case])
                mock_scraper.close = AsyncMock()
                mock_get_scraper.return_value = mock_scraper

                with patch(
                    "app.workers.sync_scheduler.detect_and_sync_movements",
                    new_callable=AsyncMock,
                ) as mock_detect:
                    mock_detect.return_value = (3, 2, [])  # 3 new movements

                    with patch("app.workers.sync_scheduler.SyncService") as mock_svc_cls:
                        mock_svc = MagicMock()
                        mock_result = MagicMock()
                        mock_result.cases_total = 1
                        mock_result.cases_new = 0
                        mock_svc.sync_cases.return_value = mock_result
                        mock_svc_cls.return_value = mock_svc

                        result = await sync_lawyer_cases(
                            lawyer_id=1, competencia="civil", db=mock_db
                        )

        assert result.get("success") is True
        assert result.get("movements_new") == 3


# ===========================================================================
# S4-T4: Notification dispatch
# ===========================================================================

class TestMovementNotification:
    """S4-T4: A newly detected movement must trigger NotificationService.notify_new_movement."""

    @pytest.mark.asyncio
    async def test_new_movement_dispatches_notification(self, db):
        """detect_and_sync_movements triggers NotificationService for each new movement found."""
        from app.services.sync_service import detect_and_sync_movements

        lawyer, case = _setup_lawyer_case(db, rol="C-700-2024")

        api_cases = [_make_api_case("C-700-2024", "token-700")]
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            return_value=_make_detail_with_movements([
                {
                    "folio": "002",
                    "fecha": "10/06/2024",
                    "tipo_tramite": "Escrito",
                    "descripcion": "Escrito de reposición",
                }
            ])
        )

        with patch(
            "app.services.sync_service.NotificationService"
        ) as mock_notif_cls:
            mock_notif = MagicMock()
            mock_notif.notify_new_movement = MagicMock()
            mock_notif_cls.return_value = mock_notif

            movements_new, alerts_created, errors = await detect_and_sync_movements(
                db=db,
                scraper=mock_scraper,
                pjud_session=MagicMock(),
                lawyer_id=lawyer.id,
                api_cases=api_cases,
            )

        assert movements_new == 1
        assert alerts_created == 1
        assert not errors
        mock_notif.notify_new_movement.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_duplicate_notifications_for_existing_movement(self, db):
        """A movement that already exists in DB does NOT trigger a new notification."""
        from app.services.sync_service import detect_and_sync_movements

        lawyer, case = _setup_lawyer_case(db, rol="C-800-2024")

        api_cases = [_make_api_case("C-800-2024", "token-800")]

        # Build consistent movement data
        movement_data = [
            {
                "folio": "003",
                "fecha": "15/06/2024",
                "tipo_tramite": "Resolución",
                "descripcion": "Se hace lugar al recurso",
            }
        ]
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            return_value=_make_detail_with_movements(movement_data)
        )

        with patch("app.services.sync_service.NotificationService") as mock_notif_cls:
            mock_notif = MagicMock()
            mock_notif.notify_new_movement = MagicMock()
            mock_notif_cls.return_value = mock_notif

            # First call — inserts the movement
            await detect_and_sync_movements(
                db=db,
                scraper=mock_scraper,
                pjud_session=MagicMock(),
                lawyer_id=lawyer.id,
                api_cases=api_cases,
            )

            first_call_count = mock_notif.notify_new_movement.call_count

            # Second call — movement already exists; must be idempotent
            await detect_and_sync_movements(
                db=db,
                scraper=mock_scraper,
                pjud_session=MagicMock(),
                lawyer_id=lawyer.id,
                api_cases=api_cases,
            )

        assert first_call_count == 1
        # The second run must NOT trigger an extra notification
        assert mock_notif.notify_new_movement.call_count == 1
