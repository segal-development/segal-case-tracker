"""Tests for the per-case hard-timeout recovery path inside
detect_and_sync_movements.

The browser fetch (panel + detail) is wrapped in
``asyncio.wait_for(resilient_call("detail", ...), timeout=PER_CASE_DETAIL_TIMEOUT_SECONDS)``.
When the underlying page hangs (page.evaluate/page.goto have no native timeout),
``wait_for`` raises ``asyncio.TimeoutError``, caught by an explicit handler that:
  1. rolls back (guarded — a reaped DB connection must not re-raise),
  2. resets ``scraper._panel_loaded = False`` (force a fresh panel reload),
  3. advances ``db_case.last_detail_checked_at`` (rotate the case to the back),
  4. increments a ``consecutive_timeouts`` counter (reset to 0 on any success),
  5. logs + records the error, and
  6. aborts the whole batch once ``consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS``
     (circuit breaker, like the Shape path).

Instead of raising ``asyncio.TimeoutError`` directly (which ``resilient_call``
would RETRY via ``retry_async`` with real backoff sleeps), we shrink
``PER_CASE_DETAIL_TIMEOUT_SECONDS`` to a tiny value and make the fake
``get_case_detail`` sleep longer, so ``wait_for`` fires its own real timeout —
faithfully exercising the production cutoff in ~0.2s per hang.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

import app.services.shape_cooldown as shape_cooldown_module
from app.services.shape_cooldown import ShapeCooldown
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer


# --- Test-local timeout so wait_for fires almost immediately -----------------
TINY_TIMEOUT = 0.2
# Longer than TINY_TIMEOUT so wait_for always wins; wait_for cancels this sleep,
# so the test does NOT actually wait 5s — it waits ~TINY_TIMEOUT.
HANG_SLEEP = 5.0


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _patch_shape_cooldown(monkeypatch) -> ShapeCooldown:
    """Install a fresh, inactive ShapeCooldown so the batch's entry gate never
    short-circuits detail work (isolation from any prior Shape hit)."""
    fresh = ShapeCooldown(
        base_seconds=300.0, max_seconds=900.0, now_fn=_FakeClock()
    )
    monkeypatch.setattr(shape_cooldown_module, "_shape_cooldown", fresh)
    return fresh


def _reset_detail_circuit_breaker() -> None:
    """Defensively clear the process-global ``pjud-detail`` circuit breaker so
    state leaked from other test modules cannot open it and mask our path.
    (wait_for cancellations are BaseException, so they are never recorded as CB
    failures — this is belt-and-suspenders isolation.)"""
    try:
        from app.scrapper.pjud.resilience.circuit_breaker import (
            get_circuit_breaker,
            CircuitState,
        )

        cb = get_circuit_breaker("pjud-detail")
        cb._state = CircuitState.CLOSED
        cb._failure_count = 0
        cb._success_count = 0
        cb._half_open_calls = 0
    except Exception:
        pass


def _patch_tiny_timeout(monkeypatch) -> None:
    import app.services.sync_service as sync_service_module

    monkeypatch.setattr(
        sync_service_module, "PER_CASE_DETAIL_TIMEOUT_SECONDS", TINY_TIMEOUT
    )


def _make_api_case(rol: str, case_token: str = "token-abc") -> MagicMock:
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


def _seed_case(db, lawyer: Lawyer, court: Court, rol: str) -> Case:
    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol=rol,
        competencia="civil",
        status="active",
        last_detail_checked_at=None,
    )
    db.add(case)
    db.commit()
    return case


def _seed_lawyer_and_court(db, rut: str, court_code: str) -> tuple[Lawyer, Court]:
    lawyer = Lawyer(rut=rut, name=f"Lawyer {rut}", is_active=True)
    db.add(lawyer)
    db.flush()
    court = Court(code=court_code, name=f"Court {court_code}", region="RM", type="civil")
    db.add(court)
    db.flush()
    db.commit()
    return lawyer, court


class TestPerCaseTimeoutRecovery:
    @pytest.mark.asyncio
    async def test_single_hang_recovers_and_batch_continues(self, db, monkeypatch):
        """A hanging case must NOT abort the batch: its rotation timestamp is
        advanced, the panel cache is reset, and a later healthy case in the same
        batch is still processed (the loop continued)."""
        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        _patch_tiny_timeout(monkeypatch)
        _reset_detail_circuit_breaker()

        lawyer, court = _seed_lawyer_and_court(db, "31000001-1", "TO-COURT1")
        case_hang = _seed_case(db, lawyer, court, "C-TO-HANG")
        case_ok = _seed_case(db, lawyer, court, "C-TO-OK")

        api_hang = _make_api_case("C-TO-HANG", "token-hang")
        api_ok = _make_api_case("C-TO-OK", "token-ok")

        async def side_effect_fn(**kwargs):
            if kwargs.get("case_token") == "token-hang":
                await asyncio.sleep(HANG_SLEEP)  # cancelled by wait_for
                return _make_detail_empty()  # never reached
            return _make_detail_empty()

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=side_effect_fn)

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_hang, api_ok],
            selected_cases=[api_hang, api_ok],
        )

        # The batch did not raise; both cases were attempted.
        db.refresh(case_hang)
        db.refresh(case_ok)

        # Hung case rotated to the back (timestamp advanced despite the hang).
        assert case_hang.last_detail_checked_at is not None
        # Healthy case AFTER the hang was still processed → loop continued.
        assert case_ok.last_detail_checked_at is not None

        # Panel cache reset so the next case forces a fresh reload.
        assert mock_scraper._panel_loaded is False

        # Exactly one recorded error, for the hung case, mentioning the timeout.
        assert len(errors) == 1
        assert "timeout" in errors[0].lower()
        assert "C-TO-HANG" in errors[0]

    @pytest.mark.asyncio
    async def test_three_consecutive_hangs_abort_batch(self, db, monkeypatch):
        """MAX_CONSECUTIVE_TIMEOUTS (=3) back-to-back hangs trip the circuit
        breaker: the 4th case is never fetched and a 'consecutive' error is
        recorded."""
        from app.services.sync_service import (
            detect_and_sync_movements,
            MAX_CONSECUTIVE_TIMEOUTS,
        )

        assert MAX_CONSECUTIVE_TIMEOUTS == 3

        _patch_shape_cooldown(monkeypatch)
        _patch_tiny_timeout(monkeypatch)
        _reset_detail_circuit_breaker()

        lawyer, court = _seed_lawyer_and_court(db, "31000002-2", "TO-COURT2")
        case1 = _seed_case(db, lawyer, court, "C-TO-1")
        case2 = _seed_case(db, lawyer, court, "C-TO-2")
        case3 = _seed_case(db, lawyer, court, "C-TO-3")
        case_never = _seed_case(db, lawyer, court, "C-TO-NEVER")

        api1 = _make_api_case("C-TO-1", "token-1")
        api2 = _make_api_case("C-TO-2", "token-2")
        api3 = _make_api_case("C-TO-3", "token-3")
        api_never = _make_api_case("C-TO-NEVER", "token-never")

        fetched_tokens: list[str] = []

        async def side_effect_fn(**kwargs):
            fetched_tokens.append(kwargs.get("case_token"))
            await asyncio.sleep(HANG_SLEEP)  # every case hangs → cancelled
            return _make_detail_empty()

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=side_effect_fn)

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api1, api2, api3, api_never],
            selected_cases=[api1, api2, api3, api_never],
        )

        # The 4th case must never be attempted (batch aborted after 3 timeouts).
        assert "token-never" not in fetched_tokens
        assert len(fetched_tokens) == 3

        db.refresh(case_never)
        assert case_never.last_detail_checked_at is None

        # 3 per-case timeout errors + 1 circuit-breaker abort error.
        assert any("consecutive" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_success_between_hangs_resets_counter(self, db, monkeypatch):
        """A successful fetch resets consecutive_timeouts, so a later isolated
        hang does NOT trip the circuit breaker: hang, success, hang, success →
        all four cases processed, no 'consecutive' abort."""
        from app.services.sync_service import detect_and_sync_movements

        _patch_shape_cooldown(monkeypatch)
        _patch_tiny_timeout(monkeypatch)
        _reset_detail_circuit_breaker()

        lawyer, court = _seed_lawyer_and_court(db, "31000003-3", "TO-COURT3")
        case_h1 = _seed_case(db, lawyer, court, "C-TO-H1")
        case_s1 = _seed_case(db, lawyer, court, "C-TO-S1")
        case_h2 = _seed_case(db, lawyer, court, "C-TO-H2")
        case_s2 = _seed_case(db, lawyer, court, "C-TO-S2")

        api_h1 = _make_api_case("C-TO-H1", "token-h1")
        api_s1 = _make_api_case("C-TO-S1", "token-s1")
        api_h2 = _make_api_case("C-TO-H2", "token-h2")
        api_s2 = _make_api_case("C-TO-S2", "token-s2")

        async def side_effect_fn(**kwargs):
            token = kwargs.get("case_token")
            if token in ("token-h1", "token-h2"):
                await asyncio.sleep(HANG_SLEEP)  # cancelled by wait_for
                return _make_detail_empty()
            return _make_detail_empty()

        mock_scraper = MagicMock()
        mock_scraper.get_case_detail = AsyncMock(side_effect=side_effect_fn)

        movements_new, alerts_created, errors = await detect_and_sync_movements(
            db=db,
            scraper=mock_scraper,
            pjud_session=MagicMock(),
            lawyer_id=lawyer.id,
            api_cases=[api_h1, api_s1, api_h2, api_s2],
            selected_cases=[api_h1, api_s1, api_h2, api_s2],
        )

        # No circuit-breaker abort: the interleaved successes reset the counter.
        assert not any("consecutive" in e.lower() for e in errors)

        # All four cases were processed (loop never aborted).
        for case in (case_h1, case_s1, case_h2, case_s2):
            db.refresh(case)
            assert case.last_detail_checked_at is not None

        # Two isolated timeouts recorded, one per hung case.
        timeout_errors = [e for e in errors if "timeout" in e.lower()]
        assert len(timeout_errors) == 2
