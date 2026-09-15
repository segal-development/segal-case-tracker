"""detect_and_sync_movements — PJUD per-account detail throttle + proactive rotation.

Verified in production (worker log 2026-09-15): PJUD limits the detail endpoint
per ACCOUNT after ~60 min of continuous detail scraping. The modal comes back
as an empty shell (``SessionExpiredError``), a fresh re-auth succeeds, and the
SAME case comes back empty again — while a different lawyer fetches details
fine seconds later. That is not a session problem, so the batch must stop with
a throttle reason, without a second re-auth and without touching the vault.

To avoid hitting the limit at all, the detail loop rotates proactively once
``DETAIL_BATCH_MAX_SECONDS`` (55 min) have elapsed since the batch started.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.shape_cooldown as shape_cooldown_module
import app.services.sync_service as sync_service_module
from app.services.shape_cooldown import ShapeCooldown
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


def _patch_shape_cooldown(monkeypatch) -> ShapeCooldown:
    fresh = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=lambda: 0.0)
    monkeypatch.setattr(shape_cooldown_module, "_shape_cooldown", fresh)
    return fresh


def _make_api_case(rol: str, case_token: str = "token") -> MagicMock:
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


def _seed(db, rut: str, court_code: str, roles: list[str]) -> tuple[Lawyer, list[Case]]:
    lawyer = Lawyer(rut=rut, name=f"Lawyer {rut}", is_active=True)
    db.add(lawyer)
    db.flush()
    court = Court(code=court_code, name=f"Court {court_code}", region="RM", type="civil")
    db.add(court)
    db.flush()
    cases = [
        Case(
            lawyer_id=lawyer.id, court_id=court.id, rol=rol, competencia="civil",
            status="active", last_detail_checked_at=None,
        )
        for rol in roles
    ]
    db.add_all(cases)
    db.commit()
    return lawyer, cases


def _empty_shell_expired():
    from app.scrapper.pjud.exceptions import SessionExpiredError

    return SessionExpiredError("detail modal is an empty shell (276 chars): session expired")


def _login_page_not_authenticated():
    from app.scrapper.pjud.exceptions import SessionNotAuthenticatedError

    return SessionNotAuthenticatedError(
        url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
        jquery_present=False,
        looks_like_login=True,
    )


class _FakeClock:
    """Monotonic clock stub: returns ``readings`` in order, then repeats the last."""

    def __init__(self, readings: list[float]):
        self._readings = list(readings)
        self.calls = 0

    def __call__(self) -> float:
        idx = min(self.calls, len(self._readings) - 1)
        self.calls += 1
        return self._readings[idx]


@pytest.fixture
def no_sleep():
    with patch("app.services.sync_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        yield mock_sleep


async def _passthrough_resilient_call(operation, factory):
    return await factory()


@pytest.fixture(autouse=True)
def bypass_detail_circuit_breaker():
    with patch(
        "app.scrapper.pjud.resilience.integration.resilient_call",
        side_effect=_passthrough_resilient_call,
    ):
        yield


class TestAccountThrottleAfterSuccessfulReauth:
    @pytest.mark.asyncio
    async def test_empty_shell_after_fresh_reauth_stops_batch_with_throttle_reason(
        self, db, monkeypatch, no_sleep
    ):
        from app.services.sync_service import (
            PJUD_DETAIL_THROTTLE_REASON,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        lawyer, cases = _seed(db, "32000001-1", "TH-COURT1", ["C-THR-1", "C-THR-2", "C-THR-3"])
        api_cases = [_make_api_case(f"C-THR-{i}", f"t{i}") for i in (1, 2, 3)]

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[_empty_shell_expired(), _empty_shell_expired()]
        )
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases, reauth_callback=reauth,
        )

        # Exactly ONE re-auth (the vault is only reachable through the
        # scheduler's reauth callback, so a single successful call means no
        # validation_failed can have been recorded).
        reauth.assert_awaited_once()
        # First attempt + one retry on the same case; nothing else fetched.
        assert scraper.get_case_detail.await_count == 2
        assert errors == [PJUD_DETAIL_THROTTLE_REASON]
        assert "lote detenido" in PJUD_DETAIL_THROTTLE_REASON
        # Not the case's fault: rotation position preserved for every case.
        for case in cases:
            db.refresh(case)
            assert case.last_detail_checked_at is None

    @pytest.mark.asyncio
    async def test_throttle_is_logged_as_warning_not_error(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        lawyer, _ = _seed(db, "32000002-2", "TH-COURT2", ["C-THR-W"])
        api_case = _make_api_case("C-THR-W", "tw")

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[_empty_shell_expired(), _empty_shell_expired()]
        )
        # Spy the module logger directly: independent of whatever handler/level
        # state other tests leave on the logging tree.
        log_spy = MagicMock()
        monkeypatch.setattr(sync_service_module, "logger", log_spy)

        await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=[api_case], selected_cases=[api_case],
            reauth_callback=AsyncMock(return_value=MagicMock()),
        )

        warning_msgs = [str(c.args[0]) for c in log_spy.warning.call_args_list]
        error_msgs = [str(c.args[0]) for c in log_spy.error.call_args_list]
        assert any("throttle" in m.lower() for m in warning_msgs)
        assert not any("throttle" in m.lower() for m in error_msgs)
        assert not any("second session expiry" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_genuine_auth_failure_on_retry_keeps_session_reason(
        self, db, monkeypatch, no_sleep
    ):
        """Regression: retry failing with SessionNotAuthenticatedError (login
        page) is a real auth problem and keeps today's handling."""
        from app.services.sync_service import (
            PJUD_DETAIL_THROTTLE_REASON,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        lawyer, _ = _seed(db, "32000003-3", "TH-COURT3", ["C-AUTH-A", "C-AUTH-B"])
        api_cases = [_make_api_case("C-AUTH-A", "ta"), _make_api_case("C-AUTH-B", "tb")]

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[_empty_shell_expired(), _login_page_not_authenticated()]
        )
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases, reauth_callback=reauth,
        )

        reauth.assert_awaited_once()
        assert scraper.get_case_detail.await_count == 2
        assert len(errors) == 1
        assert "Session expired again" in errors[0]
        assert PJUD_DETAIL_THROTTLE_REASON not in errors


class TestProactiveRotationAtCap:
    @pytest.mark.asyncio
    async def test_stops_before_next_case_once_cap_elapsed(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import (
            DETAIL_BATCH_MAX_SECONDS,
            DETAIL_ROTATION_REASON_TEMPLATE,
            detect_and_sync_movements,
        )

        assert DETAIL_BATCH_MAX_SECONDS == 55 * 60

        _patch_shape_cooldown(monkeypatch)
        lawyer, cases = _seed(db, "32000004-4", "TH-COURT4", ["C-ROT-1", "C-ROT-2"])
        api_cases = [_make_api_case("C-ROT-1", "r1"), _make_api_case("C-ROT-2", "r2")]

        # Batch start at 0; the first per-case check sees 56 min elapsed.
        clock = _FakeClock([0.0, 56 * 60.0])
        monkeypatch.setattr(sync_service_module, "DETAIL_BATCH_CLOCK", clock)

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases,
            reauth_callback=AsyncMock(),
        )

        scraper.get_case_detail.assert_not_awaited()
        assert errors == [DETAIL_ROTATION_REASON_TEMPLATE.format(minutes=56)]
        assert "56 min" in errors[0]
        for case in cases:
            db.refresh(case)
            assert case.last_detail_checked_at is None

    @pytest.mark.asyncio
    async def test_proceeds_under_cap(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        lawyer, cases = _seed(db, "32000005-5", "TH-COURT5", ["C-ROT-U1", "C-ROT-U2"])
        api_cases = [_make_api_case("C-ROT-U1", "u1"), _make_api_case("C-ROT-U2", "u2")]

        clock = _FakeClock([0.0, 10 * 60.0])
        monkeypatch.setattr(sync_service_module, "DETAIL_BATCH_CLOCK", clock)

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases,
            reauth_callback=AsyncMock(),
        )

        assert errors == []
        assert scraper.get_case_detail.await_count == 2

    @pytest.mark.asyncio
    async def test_cap_is_checked_per_case_so_batch_can_stop_midway(
        self, db, monkeypatch, no_sleep
    ):
        from app.services.sync_service import (
            DETAIL_ROTATION_REASON_TEMPLATE,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        roles = ["C-ROT-M1", "C-ROT-M2", "C-ROT-M3"]
        lawyer, cases = _seed(db, "32000006-6", "TH-COURT6", roles)
        api_cases = [_make_api_case(rol, f"m{i}") for i, rol in enumerate(roles)]

        # start=0; case1 check=0; case2 check=30 min; case3 check=57 min.
        clock = _FakeClock([0.0, 0.0, 30 * 60.0, 57 * 60.0])
        monkeypatch.setattr(sync_service_module, "DETAIL_BATCH_CLOCK", clock)

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases,
            reauth_callback=AsyncMock(),
        )

        assert scraper.get_case_detail.await_count == 2
        assert errors == [DETAIL_ROTATION_REASON_TEMPLATE.format(minutes=57)]
        db.refresh(cases[2])
        assert cases[2].last_detail_checked_at is None
        db.refresh(cases[0])
        assert cases[0].last_detail_checked_at is not None
