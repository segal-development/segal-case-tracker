"""Tests for anti-Shape-block pacing: ShapeChallengeError handling and the
station-wide Shape cooldown gate inside detect_and_sync_movements.

Contrast with tests/services/test_detail_rotation.py::TestReauthMidBatch:
a genuine SessionExpiredError/SessionNotAuthenticatedError still goes through
reauth_callback + one retry (unchanged). A ShapeChallengeError — even though
it subclasses SessionNotAuthenticatedError — must be caught SEPARATELY:
no reauth, no retry, trip the station-wide cooldown, abort the batch.
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import app.services.shape_cooldown as shape_cooldown_module
from app.services.shape_cooldown import ShapeCooldown
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _patch_shape_cooldown(monkeypatch, **kwargs) -> ShapeCooldown:
    """Replace the module-global ShapeCooldown singleton with a fresh,
    fully-controlled instance for test isolation."""
    kwargs.setdefault("base_seconds", 300.0)
    kwargs.setdefault("max_seconds", 900.0)
    kwargs.setdefault("now_fn", _FakeClock())
    fresh = ShapeCooldown(**kwargs)
    monkeypatch.setattr(shape_cooldown_module, "_shape_cooldown", fresh)
    return fresh


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


def _seed_lawyer_court_case(db, rut: str, court_code: str, rol: str) -> tuple[Lawyer, Case]:
    lawyer = Lawyer(rut=rut, name=f"Lawyer {rut}", is_active=True)
    db.add(lawyer)
    db.flush()
    court = Court(code=court_code, name=f"Court {court_code}", region="RM", type="civil")
    db.add(court)
    db.flush()
    case = Case(
        lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
        status="active", last_detail_checked_at=None,
    )
    db.add(case)
    db.commit()
    return lawyer, case


class TestShapeChallengeErrorHandling:
    @pytest.mark.asyncio
    async def test_shape_challenge_does_not_call_reauth(self, db, monkeypatch):
        """A ShapeChallengeError must NOT invoke reauth_callback."""
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        _patch_shape_cooldown(monkeypatch)

        lawyer, case = _seed_lawyer_court_case(db, "30000001-1", "SHAPE-COURT1", "C-SHAPE-1")
        api_case = _make_api_case("C-SHAPE-1", "token-shape-1")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=ShapeChallengeError(
                url="https://oficinajudicialvirtual.pjud.cl/",
                jquery_present=False,
                looks_like_login=False,
                marker="tspd_challenge",
            )
        )
        reauth_callback = AsyncMock(return_value=MagicMock())

        await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
            reauth_callback=reauth_callback,
        )

        reauth_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shape_challenge_aborts_batch_and_preserves_prior_progress(self, db, monkeypatch):
        """First case succeeds, second raises ShapeChallengeError → batch stops,
        third case is never attempted, and movements synced before the block
        are still returned."""
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        _patch_shape_cooldown(monkeypatch)

        lawyer = Lawyer(rut="30000002-2", name="Lawyer Shape2", is_active=True)
        db.add(lawyer)
        db.flush()
        court = Court(code="SHAPE-COURT2", name="Shape Court 2", region="RM", type="civil")
        db.add(court)
        db.flush()
        case_ok = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-SHAPE-OK", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_blocked = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-SHAPE-BLOCKED", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        case_never = Case(
            lawyer_id=lawyer.id, court_id=court.id, rol="C-SHAPE-NEVER", competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        db.add_all([case_ok, case_blocked, case_never])
        db.commit()

        api_ok = _make_api_case("C-SHAPE-OK", "token-ok")
        api_blocked = _make_api_case("C-SHAPE-BLOCKED", "token-blocked")
        api_never = _make_api_case("C-SHAPE-NEVER", "token-never")

        call_count = 0

        async def side_effect_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_detail_empty()
            raise ShapeChallengeError(
                url="https://oficinajudicialvirtual.pjud.cl/",
                jquery_present=False,
                looks_like_login=False,
                marker="tspd_challenge",
            )

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=side_effect_fn)

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_ok, api_blocked, api_never],
            selected_cases=[api_ok, api_blocked, api_never],
        )

        # Only 2 calls: the successful one + the one that hit the Shape challenge.
        # case_never must never be attempted.
        assert mock_scraper.get_case_detail.await_count == 2
        assert len(errors) == 1
        assert "shape" in errors[0].lower() or "tspd" in errors[0].lower()

        db.refresh(case_ok)
        db.refresh(case_never)
        assert case_ok.last_detail_checked_at is not None, "progress before the block must be preserved"
        assert case_never.last_detail_checked_at is None, "batch must abort before reaching later cases"

    @pytest.mark.asyncio
    async def test_shape_challenge_trips_station_wide_cooldown(self, db, monkeypatch):
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import ShapeChallengeError

        cooldown = _patch_shape_cooldown(monkeypatch)
        assert cooldown.active() is False

        lawyer, case = _seed_lawyer_court_case(db, "30000003-3", "SHAPE-COURT3", "C-SHAPE-3")
        api_case = _make_api_case("C-SHAPE-3", "token-shape-3")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=ShapeChallengeError(
                url="https://oficinajudicialvirtual.pjud.cl/",
                jquery_present=False,
                looks_like_login=False,
                marker="tspd_challenge",
            )
        )

        await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
        )

        assert cooldown.active() is True
        assert cooldown.consecutive == 1

    @pytest.mark.asyncio
    async def test_session_expired_error_still_goes_through_reauth_retry(self, db, monkeypatch):
        """Regression guard: a plain SessionExpiredError (NOT a Shape challenge)
        must still trigger reauth_callback + retry, unchanged."""
        from app.services.sync_service import detect_and_sync_movements
        from app.scrapper.pjud.exceptions import SessionExpiredError

        _patch_shape_cooldown(monkeypatch)

        lawyer, case = _seed_lawyer_court_case(db, "30000004-4", "SHAPE-COURT4", "C-SHAPE-4")
        api_case = _make_api_case("C-SHAPE-4", "token-shape-4")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(
            side_effect=[SessionExpiredError("expired"), _make_detail_empty()]
        )
        reauth_callback = AsyncMock(return_value=MagicMock())

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
            reauth_callback=reauth_callback,
        )

        assert mock_scraper.get_case_detail.await_count == 2
        reauth_callback.assert_awaited_once()
        assert errors == []


class TestShapeCooldownGateOnEntry:
    @pytest.mark.asyncio
    async def test_active_cooldown_skips_detail_work_no_scraper_calls(self, db, monkeypatch):
        cooldown = _patch_shape_cooldown(monkeypatch)
        cooldown.trip()
        assert cooldown.active() is True

        from app.services.sync_service import detect_and_sync_movements

        lawyer, case = _seed_lawyer_court_case(db, "30000005-5", "SHAPE-COURT5", "C-SHAPE-5")
        api_case = _make_api_case("C-SHAPE-5", "token-shape-5")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
        )

        mock_scraper.get_case_detail.assert_not_awaited()
        assert movements_new == 0
        assert alerts_created == 0
        assert errors == []


class TestShapeCooldownClearedOnSuccess:
    @pytest.mark.asyncio
    async def test_successful_detail_fetch_clears_cooldown_consecutive_count(self, db, monkeypatch):
        cooldown = _patch_shape_cooldown(monkeypatch)
        # Simulate a previous Shape hit whose cooldown window has since expired,
        # but whose consecutive counter has not yet been reset.
        cooldown.trip()
        cooldown.now_fn.advance(10_000.0)
        assert cooldown.active() is False
        assert cooldown.consecutive == 1

        from app.services.sync_service import detect_and_sync_movements

        lawyer, case = _seed_lawyer_court_case(db, "30000006-6", "SHAPE-COURT6", "C-SHAPE-6")
        api_case = _make_api_case("C-SHAPE-6", "token-shape-6")

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_case],
            selected_cases=[api_case],
        )

        assert cooldown.consecutive == 0
