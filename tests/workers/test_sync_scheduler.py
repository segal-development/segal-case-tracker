"""S1-T10: Tests for sync_scheduler — async session lookup."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSyncLawyerCasesSessionLookup:
    """sync_lawyer_cases must await store.get_session_by_lawyer."""

    @pytest.mark.asyncio
    async def test_skip_when_no_session(self):
        """sync_lawyer_cases skips when session is None."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            mock_store.get_session_by_lawyer = AsyncMock(return_value=None)
            mock_store_fn.return_value = mock_store

            result = await sync_lawyer_cases(lawyer_id=1, competencia="civil", db=mock_db)

        assert result.get("skipped") is True
        assert "no_session" in result.get("reason", "")
        mock_store.get_session_by_lawyer.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_session_by_lawyer_is_awaited(self):
        """get_session_by_lawyer must be awaited (not called synchronously)."""
        from app.workers.sync_scheduler import sync_lawyer_cases

        mock_db = MagicMock()

        with patch("app.workers.sync_scheduler.get_session_store") as mock_store_fn:
            mock_store = MagicMock()
            # Use AsyncMock so we can verify it was awaited
            mock_store.get_session_by_lawyer = AsyncMock(return_value=None)
            mock_store_fn.return_value = mock_store

            await sync_lawyer_cases(lawyer_id=99, competencia="civil", db=mock_db)

            # Ensure it was awaited, not just called
            assert mock_store.get_session_by_lawyer.await_count == 1
