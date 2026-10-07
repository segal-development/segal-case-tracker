"""Per-cycle time accounting: exclusive buckets, no-op outside a cycle, and the
two real instrumentation points that live outside the scheduler (SMTP send and
document download)."""
import smtplib
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.cycle_timing as cycle_timing
from app.services.cycle_timing import CycleTimer, timed, use_timer


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(cycle_timing, "_clock", fake)
    return fake


class TestExclusiveBuckets:
    def test_nested_bucket_time_is_not_counted_twice(self, clock):
        timer = CycleTimer()
        with use_timer(timer):
            with timed("detail"):
                clock.advance(100)
                with timed("documents"):
                    clock.advance(40)
                clock.advance(10)

        assert timer.totals["detail"] == pytest.approx(110)
        assert timer.totals["documents"] == pytest.approx(40)

    def test_unaccounted_time_lands_in_other(self, clock):
        timer = CycleTimer()
        with use_timer(timer):
            clock.advance(25)
            with timed("listing"):
                clock.advance(60)

        breakdown = timer.breakdown()
        assert breakdown["listing"] == pytest.approx(60)
        assert breakdown["other"] == pytest.approx(25)

    def test_exception_inside_bucket_is_still_charged_and_stack_recovers(self, clock):
        timer = CycleTimer()
        with use_timer(timer):
            with pytest.raises(RuntimeError):
                with timed("listing"):
                    clock.advance(30)
                    raise RuntimeError("boom")
            clock.advance(5)  # outside any bucket now
            with timed("email"):
                clock.advance(7)

        assert timer.totals["listing"] == pytest.approx(30)
        assert timer.totals["email"] == pytest.approx(7)
        assert timer.breakdown()["other"] == pytest.approx(5)

    def test_without_an_active_timer_timed_is_a_noop(self, clock):
        with timed("listing"):
            clock.advance(10)  # must not raise nor record anywhere


class TestInstrumentationPoints:
    def test_smtp_send_time_is_charged_to_email(self, clock, monkeypatch):
        from app.services.notification_service import NotificationService
        from app.config import settings

        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.invalid")
        monkeypatch.setattr(settings, "SMTP_USER", "")
        monkeypatch.setattr(settings, "SMTP_USE_TLS", False)

        server = MagicMock()
        server.send_message.side_effect = lambda msg: clock.advance(12)
        smtp_cm = MagicMock()
        smtp_cm.__enter__.return_value = server
        monkeypatch.setattr(smtplib, "SMTP", MagicMock(return_value=smtp_cm))

        alert = MagicMock(title="t", message="m", id=1)
        lawyer = MagicMock(email="a@example.com", id=1)

        timer = CycleTimer()
        with use_timer(timer):
            assert NotificationService(MagicMock()).send_email_alert(alert, lawyer) is True

        assert timer.totals["email"] == pytest.approx(12)

    @pytest.mark.asyncio
    async def test_document_download_time_is_charged_to_documents(self, clock, monkeypatch):
        from app.services.document_downloader import DocumentDownloader

        fake_bucket = MagicMock()
        fake_bucket.acquire = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "app.scrapper.pjud.resilience.rate_limiter.pjud_action_limiter",
            MagicMock(return_value=fake_bucket),
        )

        async def slow_download(**kwargs):
            clock.advance(33)
            return b"%PDF-1.4"

        scraper = MagicMock()
        scraper.download_document_generic = AsyncMock(side_effect=slow_download)
        doc = MagicMock(id=1, status="pending", pjud_endpoint="e", pjud_token="t",
                        doc_type="resolution")
        limiter = MagicMock()
        limiter.wait = AsyncMock()

        async def passthrough(operation, factory):
            return await factory()

        monkeypatch.setattr(
            "app.scrapper.pjud.resilience.integration.resilient_call", passthrough
        )

        timer = CycleTimer()
        with use_timer(timer):
            await DocumentDownloader().download_and_store(
                pending_docs=[doc], scraper=scraper, pjud_session=MagicMock(),
                db=MagicMock(), storage_service=MagicMock(), limiter=limiter,
                enabled=True,
            )

        assert timer.totals["documents"] == pytest.approx(33)


class TestProgressSurvivesAKilledProcess:
    """The summary line lives in a ``finally``, so it survives an exception --
    but NOT a kill. On the scraping station the Mac sleeps and the process is
    killed mid-cycle, so in practice the summary has never been emitted once:
    zero timing data from the instrumentation we shipped.

    A running breakdown emitted as the cycle advances is what leaves a trail
    when the process dies without unwinding.
    """

    def test_current_timer_is_reachable_from_inside_the_cycle(self, clock):
        timer = CycleTimer()
        assert cycle_timing.current_timer() is None
        with use_timer(timer):
            assert cycle_timing.current_timer() is timer
        assert cycle_timing.current_timer() is None

    def test_breakdown_is_readable_mid_cycle_not_only_at_the_end(self, clock):
        timer = CycleTimer()
        with use_timer(timer):
            with timed("listing"):
                clock.advance(30)
            # Read it here, as a per-lawyer progress line would: the cycle has
            # not ended and may never end.
            mid = cycle_timing.format_breakdown(timer)
            assert "listing=" in mid
            with timed("detail"):
                clock.advance(90)
            later = cycle_timing.format_breakdown(timer)

        # Every bucket is always printed, zero included, so presence proves
        # nothing: the VALUE is what has to move as the cycle advances.
        assert "detail=0s" in mid
        assert "detail=1m30s" in later
