"""Tests for sync_scheduler — session lookup, re-auth wiring, and failed-path bug fixes.

S1-T10: get_session_by_lawyer is awaited (not called synchronously).
S3-T7:  SyncHistory.cases_found == 0 on a failed scrape (never NULL/unset).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.sync_history import SyncHistory


class TestSyncLawyerCasesSessionLookup:
    """sync_lawyer_cases must await store.get_session_by_lawyer (S1-T10)."""

    @pytest.mark.asyncio
    async def test_skip_when_no_session_and_reauth_fails(self):
        """sync_lawyer_cases skips when session is None and re-auth also fails."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()
        mock_lawyer = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_lawyer

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session_by_lawyer = AsyncMock(return_value=None)
            mock_store_fn.return_value = mock_store

            # Patch _reauth so the test is isolated from credential logic
            with patch(
                "app.workers.sync_scheduler._reauth", new_callable=AsyncMock
            ) as mock_reauth:
                mock_reauth.return_value = (None, "no_session")

                result = await sync_lawyer_cases(
                    lawyer_id=1, competencia="civil", db=mock_db
                )

        assert result.get("skipped") is True
        assert "no_session" in result.get("reason", "")
        mock_store.get_session_by_lawyer.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_session_by_lawyer_is_awaited(self):
        """get_session_by_lawyer must be awaited (not just called synchronously)."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()
        mock_lawyer = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_lawyer

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session_by_lawyer = AsyncMock(return_value=None)
            mock_store_fn.return_value = mock_store

            with patch(
                "app.workers.sync_scheduler._reauth", new_callable=AsyncMock
            ) as mock_reauth:
                mock_reauth.return_value = (None, "no_session")

                await sync_lawyer_cases(lawyer_id=99, competencia="civil", db=mock_db)

                # Ensure it was awaited, not just called
                assert mock_store.get_session_by_lawyer.await_count == 1


class TestSyncLawyerCasesCasesFound:
    """S3-T8: SyncHistory.cases_found is explicitly 0 on failure (not unset/null)."""

    @pytest.mark.asyncio
    async def test_failed_scrape_records_cases_found_zero(self):
        """When scraper.get_my_cases raises, SyncHistory has cases_found == 0."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()
        added_records: list = []
        mock_db.add.side_effect = lambda r: added_records.append(r)

        # Return a live session so we bypass the re-auth path
        mock_pjud_session = MagicMock()

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session_by_lawyer = AsyncMock(return_value=mock_pjud_session)
            mock_store_fn.return_value = mock_store

            with patch("app.api.v1.pjud.get_scraper") as mock_get_scraper:
                mock_scraper = MagicMock()
                mock_scraper.get_my_cases = AsyncMock(
                    side_effect=Exception("PJUD scrape failed")
                )
                mock_scraper.close = AsyncMock()
                mock_get_scraper.return_value = mock_scraper

                result = await sync_lawyer_cases(
                    lawyer_id=1, competencia="civil", db=mock_db
                )

        assert result.get("success") is False

        sync_records = [r for r in added_records if isinstance(r, SyncHistory)]
        assert len(sync_records) == 1, "Expected exactly one SyncHistory record"
        assert sync_records[0].cases_found == 0, (
            f"cases_found should be 0, got {sync_records[0].cases_found!r}"
        )
