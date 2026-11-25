"""consulta_by_rol: clean page state between consultas and empty-ROL detail handling."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.exceptions import DetailEmptyError, ScrapingError

_INDEX_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
_SEARCH = "<a onClick=\"detalleCausaCivil('JWT1')\">Detalle</a>"
_EMPTY_MODAL = "<div id='historiaCiv'></div>"
_GOOD_MODAL = "<strong>ROL:</strong> C-1-2023 <div id='historiaCiv'></div>"


def _page(evaluate_returns):
    page = AsyncMock()
    page.url = _INDEX_URL
    page.evaluate = AsyncMock(side_effect=evaluate_returns)
    return page


def _scraper(monkeypatch, page):
    sc = CivilScraper(headless=True)
    monkeypatch.setattr(sc, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())
    return sc


def test_detail_empty_error_is_a_scraping_error():
    assert issubclass(DetailEmptyError, ScrapingError)


async def test_cuaderno_timeout_restores_clean_page(monkeypatch):
    sc = CivilScraper(headless=True)
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=[["a", "b", "c"]])
    page.select_option = AsyncMock(side_effect=TimeoutError("Timeout 8000ms"))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())
    detail = MagicMock()
    detail.movements = []

    await sc._load_other_cuadernos(page, detail)  # never fatal

    page.goto.assert_awaited_once()
    assert "indexN.php" in page.goto.await_args.args[0]
    page.select_option.assert_awaited_once()  # broken page: stop trying more cuadernos


async def test_failed_reload_after_cuaderno_timeout_forces_goto_on_next_consulta(monkeypatch):
    page = _page(["", "<tr><td>No se han encontrado</td></tr>"])
    sc = _scraper(monkeypatch, page)
    sc._consulta_needs_reload = True

    assert await sc.consulta_by_rol(MagicMock(), "C-1-2023", corte="90") is None

    page.goto.assert_awaited_once()


async def test_every_consulta_clears_previous_modal(monkeypatch):
    page = _page(["", "<tr><td>No se han encontrado</td></tr>"])
    sc = _scraper(monkeypatch, page)

    await sc.consulta_by_rol(MagicMock(), "C-1-2023", corte="90")

    first_js = page.evaluate.await_args_list[0].args[0]
    assert "#modalDetalleCivil" in first_js and "innerHTML = ''" in first_js
    page.goto.assert_not_awaited()  # normal path: no extra navigation


async def test_empty_rol_detail_retried_once_then_succeeds(monkeypatch):
    page = _page([
        "", _SEARCH, "", _EMPTY_MODAL,       # attempt 1 → no ROL
        "", _SEARCH, "", _GOOD_MODAL,        # attempt 2 → ok
        [],                                   # _load_other_cuadernos
    ])
    sc = _scraper(monkeypatch, page)

    detail = await sc.consulta_by_rol(MagicMock(), "C-1-2023", corte="90")

    assert detail.case.rol == "C-1-2023"
    page.goto.assert_awaited_once()  # reloaded between attempts


async def test_empty_rol_detail_twice_raises_detail_empty(monkeypatch):
    page = _page(["", _SEARCH, "", _EMPTY_MODAL] * 2)
    sc = _scraper(monkeypatch, page)

    with pytest.raises(DetailEmptyError):
        await sc.consulta_by_rol(MagicMock(), "C-1-2023", corte="90")

    assert page.evaluate.await_count == 8  # exactly one retry


async def test_bad_rol_format_with_rol_present_is_not_retried(monkeypatch):
    bad = "<strong>ROL:</strong> weird <div id='historiaCiv'></div>"
    page = _page(["", _SEARCH, "", bad])
    sc = _scraper(monkeypatch, page)

    with pytest.raises(ValueError):
        await sc.consulta_by_rol(MagicMock(), "C-1-2023", corte="90")

    assert page.evaluate.await_count == 4
