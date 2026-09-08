"""detect_and_sync_movements — transient navigation failures (Defect B).

A ``TransientNavigationError`` (or a raw Playwright network/timeout error) means
the page did not load; the credential is fine. The batch must retry the SAME
case with backoff WITHOUT re-authenticating, count one error if it still
fails, continue with the next case, and only stop after several consecutive
transient failures — with a reason that names the network, not the session.

Genuine session errors keep the existing behaviour (one re-auth, one retry,
abort on a second failure) — see tests/services/test_detail_rotation.py.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.shape_cooldown as shape_cooldown_module
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


def _transient(rol: str = "C-X"):
    from app.scrapper.pjud.exceptions import TransientNavigationError

    return TransientNavigationError(
        url="https://oficinajudicialvirtual.pjud.cl/indexN.php",
        reason=f"net::ERR_NAME_NOT_RESOLVED ({rol})",
    )


@pytest.fixture
def no_sleep():
    with patch("app.services.sync_service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        yield mock_sleep


async def _passthrough_resilient_call(operation, factory):
    return await factory()


@pytest.fixture(autouse=True)
def bypass_detail_circuit_breaker():
    """Keep the pjud-detail circuit breaker out of these counts: repeated
    failures would open it mid-test and turn later cases into CircuitOpenError.
    The breaker's own interplay is covered by TestCircuitOpenIsTransient."""
    with patch(
        "app.scrapper.pjud.resilience.integration.resilient_call",
        side_effect=_passthrough_resilient_call,
    ):
        yield


class TestTransientRetrySameCase:
    @pytest.mark.asyncio
    async def test_transient_twice_then_success_no_reauth_no_errors(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import (
            TRANSIENT_NAV_BACKOFF_SECONDS,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        lawyer, (case,) = _seed(db, "31000001-1", "TN-COURT1", ["C-TN-1"])
        api_case = _make_api_case("C-TN-1")

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[_transient(), _transient(), _make_detail_empty()]
        )
        reauth = AsyncMock(return_value=MagicMock())

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=[api_case], selected_cases=[api_case], reauth_callback=reauth,
        )

        assert errors == []
        reauth.assert_not_awaited()
        assert scraper.get_case_detail.await_count == 3
        # Backoff between attempts: 10s then 20s.
        slept = [c.args[0] for c in no_sleep.await_args_list]
        assert slept[:2] == list(TRANSIENT_NAV_BACKOFF_SECONDS[:2])
        db.refresh(case)
        assert case.last_detail_checked_at is not None

    @pytest.mark.asyncio
    async def test_playwright_network_error_is_treated_as_transient(self, db, monkeypatch, no_sleep):
        """A raw Playwright error with a net::ERR_ / Timeout text gets the same treatment."""
        from playwright.async_api import Error as PlaywrightError

        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        lawyer, _ = _seed(db, "31000002-2", "TN-COURT2", ["C-TN-2"])
        api_case = _make_api_case("C-TN-2")

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[
                PlaywrightError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x"),
                PlaywrightError("Page.goto: Timeout 20000ms exceeded."),
                _make_detail_empty(),
            ]
        )
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=[api_case], selected_cases=[api_case], reauth_callback=reauth,
        )

        assert errors == []
        reauth.assert_not_awaited()
        assert scraper.get_case_detail.await_count == 3

    @pytest.mark.asyncio
    async def test_transient_exhausted_counts_one_error_and_continues(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import (
            TRANSIENT_NAV_MAX_ATTEMPTS,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        lawyer, (case_bad, case_ok) = _seed(
            db, "31000003-3", "TN-COURT3", ["C-TN-BAD", "C-TN-OK"]
        )
        api_bad = _make_api_case("C-TN-BAD", "tok-bad")
        api_ok = _make_api_case("C-TN-OK", "tok-ok")

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[_transient("C-TN-BAD")] * TRANSIENT_NAV_MAX_ATTEMPTS
            + [_make_detail_empty()]
        )
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=[api_bad, api_ok], selected_cases=[api_bad, api_ok],
            reauth_callback=reauth,
        )

        assert len(errors) == 1, errors
        assert "C-TN-BAD" in errors[0]
        assert "net::ERR_NAME_NOT_RESOLVED" in errors[0]
        assert "detenido" not in errors[0]
        reauth.assert_not_awaited()
        # 4 attempts for the bad case + 1 for the next case.
        assert scraper.get_case_detail.await_count == TRANSIENT_NAV_MAX_ATTEMPTS + 1
        db.refresh(case_ok)
        assert case_ok.last_detail_checked_at is not None
        # A network blip is not the case's fault — keep its rotation position.
        db.refresh(case_bad)
        assert case_bad.last_detail_checked_at is None


class TestConsecutiveTransientFailuresStopBatch:
    @pytest.mark.asyncio
    async def test_five_consecutive_transient_cases_stop_batch(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import (
            MAX_CONSECUTIVE_TRANSIENT_FAILURES,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        roles = [f"C-TN-S{i}" for i in range(MAX_CONSECUTIVE_TRANSIENT_FAILURES + 2)]
        lawyer, cases = _seed(db, "31000004-4", "TN-COURT4", roles)
        api_cases = [_make_api_case(r, f"tok-{r}") for r in roles]

        def _always_transient(**kwargs):
            raise _transient()

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(side_effect=_always_transient)
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases, reauth_callback=reauth,
        )

        reauth.assert_not_awaited()
        # One error per failed case + the batch-stop reason.
        assert len(errors) == MAX_CONSECUTIVE_TRANSIENT_FAILURES + 1, errors
        stop_reason = errors[-1]
        assert "no disponible" in stop_reason
        assert str(MAX_CONSECUTIVE_TRANSIENT_FAILURES) in stop_reason
        assert "lote detenido" in stop_reason
        assert "authenticated" not in " ".join(errors).lower()
        # Cases after the stop were never attempted.
        for never in cases[MAX_CONSECUTIVE_TRANSIENT_FAILURES:]:
            db.refresh(never)
            assert never.last_detail_checked_at is None

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_counter(self, db, monkeypatch, no_sleep):
        from app.services.sync_service import (
            MAX_CONSECUTIVE_TRANSIENT_FAILURES,
            TRANSIENT_NAV_MAX_ATTEMPTS,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        # N-1 failing cases, one success, N-1 failing cases → never reaches N in a row.
        n_fail = MAX_CONSECUTIVE_TRANSIENT_FAILURES - 1
        roles = [f"C-A{i}" for i in range(n_fail)] + ["C-OK"] + [f"C-B{i}" for i in range(n_fail)]
        lawyer, _ = _seed(db, "31000005-5", "TN-COURT5", roles)
        api_cases = [_make_api_case(r, f"tok-{r}") for r in roles]

        side_effects = (
            [_transient()] * (n_fail * TRANSIENT_NAV_MAX_ATTEMPTS)
            + [_make_detail_empty()]
            + [_transient()] * (n_fail * TRANSIENT_NAV_MAX_ATTEMPTS)
        )
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(side_effect=side_effects)

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases, reauth_callback=AsyncMock(),
        )

        assert len(errors) == 2 * n_fail, errors
        assert not any("detenido" in e for e in errors)


class TestGenuineAuthPathUnchanged:
    @pytest.mark.asyncio
    async def test_session_not_authenticated_reauths_once_then_succeeds(self, db, monkeypatch, no_sleep):
        from app.scrapper.pjud.exceptions import SessionNotAuthenticatedError
        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        lawyer, (case,) = _seed(db, "31000006-6", "TN-COURT6", ["C-AUTH-1"])
        api_case = _make_api_case("C-AUTH-1")

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[
                SessionNotAuthenticatedError(
                    url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
                    jquery_present=False,
                    looks_like_login=True,
                ),
                _make_detail_empty(),
            ]
        )
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=[api_case], selected_cases=[api_case], reauth_callback=reauth,
        )

        assert errors == []
        reauth.assert_awaited_once()
        assert scraper.get_case_detail.await_count == 2
        db.refresh(case)
        assert case.last_detail_checked_at is not None

    @pytest.mark.asyncio
    async def test_second_auth_failure_stops_batch_with_session_reason(self, db, monkeypatch, no_sleep):
        from app.scrapper.pjud.exceptions import SessionNotAuthenticatedError
        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        lawyer, _ = _seed(db, "31000007-7", "TN-COURT7", ["C-AUTH-2", "C-AUTH-3"])
        api_cases = [_make_api_case("C-AUTH-2", "t2"), _make_api_case("C-AUTH-3", "t3")]

        def _auth_fail(**kwargs):
            raise SessionNotAuthenticatedError(
                url="https://oficinajudicialvirtual.pjud.cl/home/index.php",
                jquery_present=False,
                looks_like_login=True,
            )

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(side_effect=_auth_fail)
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=api_cases, selected_cases=api_cases, reauth_callback=reauth,
        )

        reauth.assert_awaited_once()
        assert len(errors) == 1
        assert "Session expired again" in errors[0]
        # Second case never attempted.
        assert scraper.get_case_detail.await_count == 2


class TestCircuitOpenIsTransient:
    @pytest.mark.asyncio
    async def test_circuit_open_retries_without_reauth_and_keeps_rotation(self, db, monkeypatch, no_sleep):
        """An open pjud-detail circuit = PJUD down for everyone: retry with backoff
        (long enough to reach the half-open window), never re-auth, and do NOT
        mark the case as checked when it still fails."""
        from app.scrapper.pjud.exceptions import CircuitOpenError
        from app.services.sync_service import (
            TRANSIENT_NAV_MAX_ATTEMPTS,
            detect_and_sync_movements,
        )

        _patch_shape_cooldown(monkeypatch)
        lawyer, (case,) = _seed(db, "31000008-8", "TN-COURT8", ["C-CB-1"])
        api_case = _make_api_case("C-CB-1")

        def _open(**kwargs):
            raise CircuitOpenError("detail", 59)

        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(side_effect=_open)
        reauth = AsyncMock(return_value=MagicMock())

        _, _, errors = await detect_and_sync_movements(
            db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
            api_cases=[api_case], selected_cases=[api_case], reauth_callback=reauth,
        )

        reauth.assert_not_awaited()
        assert scraper.get_case_detail.await_count == TRANSIENT_NAV_MAX_ATTEMPTS
        assert len(errors) == 1 and "Circuit breaker open" in errors[0]
        db.refresh(case)
        assert case.last_detail_checked_at is None
