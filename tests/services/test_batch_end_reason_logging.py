"""The closing line of a detail batch must say WHY the batch ended.

Before this, a batch could stop by batch size, by the 55-minute cap, by PJUD's
~60 min account limit, by consecutive timeouts, or because nothing was left,
and the log only let you guess. Each reason now has its own explicit token.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.shape_cooldown as shape_cooldown_module
import app.services.sync_service as sync_service_module
from app.models.case import Case
from app.models.court import Court
from app.models.lawyer import Lawyer
from app.services.shape_cooldown import ShapeCooldown


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    fresh = ShapeCooldown(base_seconds=300.0, max_seconds=900.0, now_fn=lambda: 0.0)
    monkeypatch.setattr(shape_cooldown_module, "_shape_cooldown", fresh)

    async def passthrough(operation, factory):
        return await factory()

    monkeypatch.setattr(
        "app.scrapper.pjud.resilience.integration.resilient_call", passthrough
    )
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.sync_service.asyncio.sleep", sleep)
    return sleep


def _api_case(rol: str, token: str) -> MagicMock:
    m = MagicMock()
    m.rol = rol
    m.case_token = token
    return m


def _empty_detail() -> MagicMock:
    d = MagicMock()
    d.case_documents = []
    d.movements = []
    d.litigantes = []
    d.notificaciones = []
    d.escritos = []
    d.exhortos = []
    d.case = MagicMock()
    d.case.rol = "C-FAKE"
    return d


def _seed(db, rut: str, roles: list[str]):
    lawyer = Lawyer(rut=rut, name=f"L {rut}", is_active=True)
    db.add(lawyer)
    db.flush()
    court = Court(code=f"BR-{rut}", name="Court", region="RM", type="civil")
    db.add(court)
    db.flush()
    db.add_all([
        Case(lawyer_id=lawyer.id, court_id=court.id, rol=r, competencia="civil",
             status="active", last_detail_checked_at=None)
        for r in roles
    ])
    db.commit()
    return lawyer


def _end_lines(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if "end_reason=" in r.getMessage()
    ]


async def _run(db, lawyer, api_cases, scraper, **kwargs):
    return await sync_service_module.detect_and_sync_movements(
        db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
        api_cases=api_cases, selected_cases=api_cases, **kwargs,
    )


class TestBatchEndReason:
    @pytest.mark.asyncio
    async def test_batch_that_ran_out_of_cases_says_no_more_cases(self, db, caplog):
        lawyer = _seed(db, "50000001-1", ["C-1-2024", "C-2-2024"])
        api = [_api_case("C-1-2024", "a"), _api_case("C-2-2024", "b")]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_empty_detail())

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, api, scraper, batch_size_cap=30)

        lines = _end_lines(caplog)
        assert len(lines) == 1
        assert "end_reason=no_more_cases" in lines[0]
        assert "cases=2/2" in lines[0]

    @pytest.mark.asyncio
    async def test_batch_cut_by_batch_size_says_batch_size(self, db, caplog):
        lawyer = _seed(db, "50000002-2", ["C-1-2024", "C-2-2024"])
        api = [_api_case("C-1-2024", "a"), _api_case("C-2-2024", "b")]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_empty_detail())

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, api, scraper, batch_size_cap=2)

        lines = _end_lines(caplog)
        assert len(lines) == 1
        assert "end_reason=batch_size" in lines[0]

    @pytest.mark.asyncio
    async def test_batch_cut_by_time_cap_says_time_cap(self, db, caplog, monkeypatch):
        lawyer = _seed(db, "50000003-3", ["C-1-2024", "C-2-2024"])
        api = [_api_case("C-1-2024", "a"), _api_case("C-2-2024", "b")]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_empty_detail())
        readings = iter([0.0, 56 * 60.0])
        monkeypatch.setattr(
            sync_service_module, "DETAIL_BATCH_CLOCK", lambda: next(readings, 56 * 60.0)
        )

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, api, scraper, batch_size_cap=30)

        lines = _end_lines(caplog)
        assert len(lines) == 1
        assert "end_reason=time_cap" in lines[0]
        assert "cases=0/2" in lines[0]

    @pytest.mark.asyncio
    async def test_batch_cut_by_pjud_account_limit_says_so(self, db, caplog):
        from app.scrapper.pjud.exceptions import SessionExpiredError

        lawyer = _seed(db, "50000004-4", ["C-1-2024", "C-2-2024"])
        api = [_api_case("C-1-2024", "a"), _api_case("C-2-2024", "b")]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(
            side_effect=[SessionExpiredError("empty shell"), SessionExpiredError("empty shell")]
        )

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, api, scraper, reauth_callback=AsyncMock(return_value=MagicMock()),
                       batch_size_cap=30)

        lines = _end_lines(caplog)
        assert len(lines) == 1
        assert "end_reason=pjud_account_limit" in lines[0]

    @pytest.mark.asyncio
    async def test_batch_cut_by_consecutive_timeouts_says_so(self, db, caplog):
        roles = [f"C-{i}-2024" for i in range(1, 5)]
        lawyer = _seed(db, "50000005-5", roles)
        api = [_api_case(r, f"t{i}") for i, r in enumerate(roles)]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(side_effect=TimeoutError())

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, api, scraper, batch_size_cap=30)

        lines = _end_lines(caplog)
        assert len(lines) == 1
        assert "end_reason=consecutive_timeouts" in lines[0]
        assert "cases=3/4" in lines[0]

    @pytest.mark.asyncio
    async def test_empty_selection_says_no_cases(self, db, caplog):
        lawyer = _seed(db, "50000006-6", ["C-1-2024"])

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, [], MagicMock(), batch_size_cap=30)

        lines = _end_lines(caplog)
        assert len(lines) == 1
        assert "end_reason=no_cases" in lines[0]

    @pytest.mark.asyncio
    async def test_reasons_are_distinguishable_from_each_other(self, db, caplog):
        """The whole point: two different causes must not produce the same token."""
        lawyer = _seed(db, "50000007-7", ["C-1-2024", "C-2-2024"])
        api = [_api_case("C-1-2024", "a"), _api_case("C-2-2024", "b")]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_empty_detail())

        with caplog.at_level(logging.INFO):
            await _run(db, lawyer, api, scraper, batch_size_cap=30)
            await _run(db, lawyer, api, scraper, batch_size_cap=2)

        lines = _end_lines(caplog)
        assert len(lines) == 2
        assert lines[0] != lines[1]
        assert "no_more_cases" in lines[0] and "batch_size" in lines[1]


class TestBatchBehaviourIsUnchanged:
    @pytest.mark.asyncio
    async def test_same_cases_in_same_order_with_same_inter_case_delay(
        self, db, monkeypatch, _isolated
    ):
        sleep = _isolated
        monkeypatch.setattr(sync_service_module.random, "uniform", lambda a, b: 1.0)
        roles = ["C-1-2024", "C-2-2024", "C-3-2024"]
        lawyer = _seed(db, "50000008-8", roles)
        api = [_api_case(r, f"tok{i}") for i, r in enumerate(roles)]
        scraper = MagicMock()
        scraper.get_case_detail = AsyncMock(return_value=_empty_detail())

        movements, alerts, errors = await _run(
            db, lawyer, api, scraper, delay_between_fetches=2.0, batch_size_cap=3
        )

        fetched = [c.kwargs["case_token"] for c in scraper.get_case_detail.await_args_list]
        assert fetched == ["tok0", "tok1", "tok2"]
        assert [c.args[0] for c in sleep.await_args_list] == [2.0, 2.0, 2.0]
        assert (movements, alerts, errors) == (0, 0, [])
