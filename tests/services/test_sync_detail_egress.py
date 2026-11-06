# ruff: noqa: F811
"""Detail loop egress guard: stop the lawyer cleanly, no PJUD request after a forbidden result."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.sync_service as sync_service_module
from tests.services.test_detail_throttle_and_rotation import (  # noqa: F401
    _make_api_case,
    _make_detail_empty,
    _patch_shape_cooldown,
    _seed,
    bypass_detail_circuit_breaker,
    no_sleep,
)

OFFICE = "186.10.251.189"


async def _run(db, monkeypatch, verdicts, roles):
    from app.services.sync_service import detect_and_sync_movements

    _patch_shape_cooldown(monkeypatch)
    lawyer, _ = _seed(db, "32000099-9", "EG-COURT", roles)
    api_cases = [_make_api_case(r, f"t-{r}") for r in roles]
    queue = list(verdicts)
    guard = MagicMock(side_effect=lambda: queue.pop(0) if len(queue) > 1 else queue[0])
    monkeypatch.setattr(sync_service_module, "egress_allowed_cached", guard)
    scraper = MagicMock()
    scraper.get_case_detail = AsyncMock(return_value=_make_detail_empty())
    _, _, errors = await detect_and_sync_movements(
        db=db, scraper=scraper, pjud_session=MagicMock(), lawyer_id=lawyer.id,
        api_cases=api_cases, selected_cases=api_cases, reauth_callback=AsyncMock(),
    )
    return scraper, errors, guard


@pytest.mark.asyncio
async def test_forbidden_before_first_case_makes_no_pjud_request(db, monkeypatch, no_sleep):
    scraper, errors, _ = await _run(
        db, monkeypatch, [(False, OFFICE, "forbidden")], ["EG-1", "EG-2"]
    )
    scraper.get_case_detail.assert_not_awaited()
    assert errors == [f"Red no permitida: {OFFICE}"]


@pytest.mark.asyncio
async def test_becoming_forbidden_midway_stops_before_next_request(db, monkeypatch, no_sleep):
    scraper, errors, _ = await _run(
        db, monkeypatch,
        [(True, "1.2.3.4", "allowed"), (False, OFFICE, "forbidden")],
        ["EG-1", "EG-2", "EG-3"],
    )
    assert scraper.get_case_detail.await_count == 1
    assert errors[-1] == f"Red no permitida: {OFFICE}"


@pytest.mark.asyncio
async def test_unknown_ip_stops_with_desconocida(db, monkeypatch, no_sleep):
    scraper, errors, _ = await _run(db, monkeypatch, [(False, None, "unknown")], ["EG-1"])
    scraper.get_case_detail.assert_not_awaited()
    assert errors == ["Red no permitida: desconocida"]


@pytest.mark.asyncio
async def test_allowed_ip_leaves_the_batch_unchanged(db, monkeypatch, no_sleep):
    scraper, errors, _ = await _run(
        db, monkeypatch, [(True, "1.2.3.4", "allowed")], ["EG-1", "EG-2"]
    )
    assert scraper.get_case_detail.await_count == 2
    assert not any("Red no permitida" in e for e in errors)
