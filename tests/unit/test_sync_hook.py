"""Unit tests for the deadline engine sync hook.

Tests `_maybe_recompute_deadlines(db, case)` which is the isolated helper
added to sync_service.py.  This keeps the tests free of the heavy async
machinery of detect_and_sync_movements while still verifying the key contract:

  1. Calls DeadlineEngine.recompute_case once per civil case.
  2. Does NOT call it for non-civil cases (competencia != 'civil').
  3. Sync continues (no re-raise) if recompute_case raises an exception.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def civil_case():
    case = MagicMock()
    case.id = 1
    case.competencia = "civil"
    return case


@pytest.fixture()
def laboral_case():
    case = MagicMock()
    case.id = 2
    case.competencia = "laboral"
    return case


@pytest.fixture()
def mock_db():
    return MagicMock()


# ---------------------------------------------------------------------------
# Import guard: helper must exist before GREEN phase fails these tests
# ---------------------------------------------------------------------------


class TestSyncHookImport:
    """Verify the helper is importable (will fail in RED phase — expected)."""

    def test_maybe_recompute_deadlines_importable(self) -> None:
        from app.services.sync_service import _maybe_recompute_deadlines  # noqa: F401


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------


class TestMaybeRecomputeDeadlines:
    @patch("app.services.sync_service.DeadlineEngine")
    def test_calls_engine_once_for_civil_case(
        self, mock_engine, civil_case, mock_db
    ) -> None:
        """Civil competencia → recompute_case called exactly once."""
        from app.services.sync_service import _maybe_recompute_deadlines

        _maybe_recompute_deadlines(mock_db, civil_case)

        mock_engine.recompute_case.assert_called_once_with(mock_db, civil_case)

    @patch("app.services.sync_service.DeadlineEngine")
    def test_skips_non_civil_case(
        self, mock_engine, laboral_case, mock_db
    ) -> None:
        """Non-civil competencia → recompute_case NOT called."""
        from app.services.sync_service import _maybe_recompute_deadlines

        _maybe_recompute_deadlines(mock_db, laboral_case)

        mock_engine.recompute_case.assert_not_called()

    @patch("app.services.sync_service.DeadlineEngine")
    def test_sync_continues_if_recompute_raises(
        self, mock_engine, civil_case, mock_db
    ) -> None:
        """Exception from recompute_case is swallowed — sync must NOT re-raise."""
        from app.services.sync_service import _maybe_recompute_deadlines

        mock_engine.recompute_case.side_effect = RuntimeError("DB explosion")

        # Must not raise — the sync transaction must continue.
        _maybe_recompute_deadlines(mock_db, civil_case)

    @patch("app.services.sync_service.DeadlineEngine")
    def test_skips_penal_case(
        self, mock_engine, mock_db
    ) -> None:
        """Penal competencia → recompute_case NOT called."""
        from app.services.sync_service import _maybe_recompute_deadlines

        penal_case = MagicMock()
        penal_case.id = 3
        penal_case.competencia = "penal"

        _maybe_recompute_deadlines(mock_db, penal_case)

        mock_engine.recompute_case.assert_not_called()

    @patch("app.services.sync_service.DeadlineEngine")
    def test_competencia_case_insensitive(
        self, mock_engine, mock_db
    ) -> None:
        """'CIVIL' (uppercase) is treated as civil — engine is called."""
        from app.services.sync_service import _maybe_recompute_deadlines

        upper_civil_case = MagicMock()
        upper_civil_case.id = 4
        upper_civil_case.competencia = "CIVIL"

        _maybe_recompute_deadlines(mock_db, upper_civil_case)

        mock_engine.recompute_case.assert_called_once_with(mock_db, upper_civil_case)
