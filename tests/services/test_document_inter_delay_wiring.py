"""Anti-Shape pacing: detect_and_sync_movements must space document downloads
within a case using settings.DOCUMENT_INTER_DELAY, not a zero-delay limiter.

Previously the synchronous document-download step used
``AsyncSleepLimiter(delay=0.0)``, so all documents for a case fired back to
back with zero spacing on the same session as the just-fetched detail — a
Shape-tripping burst. This must now use DOCUMENT_INTER_DELAY.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


def _make_api_case(rol: str, case_token: str | None = "token-abc") -> MagicMock:
    m = MagicMock()
    m.rol = rol
    m.case_token = case_token
    return m


def _make_detail_empty() -> MagicMock:
    detail = MagicMock()
    detail.case_documents = []
    detail.movements = []
    detail.litigantes = []
    detail.notificaciones = []
    detail.escritos = []
    detail.exhortos = []
    detail.case = MagicMock()
    detail.case.rol = "C-FAKE-ROL"
    return detail


class TestDocumentInterDelayWiring:
    @pytest.mark.asyncio
    async def test_document_downloader_limiter_uses_document_inter_delay(self, db, monkeypatch):
        from app.services import sync_service
        from app.services.sync_service import detect_and_sync_movements

        monkeypatch.setattr(sync_service.settings, "DOC_DOWNLOAD_ENABLED", True)
        monkeypatch.setattr(sync_service.settings, "DOCUMENT_INTER_DELAY", 4.0)

        lawyer = Lawyer(rut="40000001-1", name="Lawyer DocDelay", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="DOCDELAY-COURT", name="DocDelay Court", region="RM", type="civil")
        db.add(court)
        db.flush()
        case = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-DOCDELAY-1", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add(case)
        db.commit()

        api_case = _make_api_case("C-DOCDELAY-1", "token-docdelay-1")
        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        fake_pending_doc = MagicMock()
        fake_pending_doc.status = "pending"

        with patch.object(
            sync_service.DocumentPersistenceService,
            "persist_from_detail",
            return_value=[fake_pending_doc],
        ), patch(
            "app.services.document_downloader.DocumentDownloader.download_and_store",
            new_callable=AsyncMock,
        ) as mock_download:
            await detect_and_sync_movements(
                db=db,
                scraper=mock_scraper,
                pjud_session=MagicMock(),
                lawyer_id=lawyer.id,
                api_cases=[api_case],
                selected_cases=[api_case],
            )

        mock_download.assert_awaited_once()
        _, kwargs = mock_download.await_args
        limiter = kwargs["limiter"]
        assert limiter._delay == 4.0, "document downloader limiter must use DOCUMENT_INTER_DELAY, not 0.0"
