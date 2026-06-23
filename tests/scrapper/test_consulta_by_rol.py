"""Unit tests for CivilScraper.consulta_by_rol — the consulta-by-rol scraping mode.

The method is also validated live against PJUD; these tests guard the branching
logic (reserved/not-found → None, found → parsed via the existing detail parser)
without touching the network. asyncio_mode=auto (no marker needed).
"""
from unittest.mock import AsyncMock, MagicMock

from app.scrapper.pjud.civil import CivilScraper

_INDEX_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"


def _mock_page(evaluate_returns):
    page = AsyncMock()
    page.url = _INDEX_URL  # already on indexN.php → skips goto
    page.evaluate = AsyncMock(side_effect=evaluate_returns)
    return page


async def test_reserved_case_returns_none(monkeypatch):
    """A reserved case (not in the consulta) → the search HTML says so → None."""
    sc = CivilScraper(headless=True)
    page = _mock_page(["", "<tr><td>No se han encontrado resultados...</td></tr>"])
    monkeypatch.setattr(sc, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())

    assert await sc.consulta_by_rol(MagicMock(), "C-7733-2026", corte="90") is None


async def test_found_case_parses_detail(monkeypatch):
    """A public case → extract the JWT, fetch detail, return the parsed result."""
    sc = CivilScraper(headless=True)
    search_html = "<a onClick=\"detalleCausaCivil('JWT123')\">Detalle</a>"
    page = _mock_page(["", search_html, "", "<div id='historiaCiv'></div>"])
    monkeypatch.setattr(sc, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())
    sentinel = object()
    monkeypatch.setattr(sc, "_parse_case_detail_html", MagicMock(return_value=sentinel))

    result = await sc.consulta_by_rol(MagicMock(), "C-7545-2025", corte="90")

    assert result is sentinel
    sc._parse_case_detail_html.assert_called_once()
    # the JWT extracted from the search HTML is passed as the case token
    assert sc._parse_case_detail_html.call_args.args[1] == "JWT123"


async def test_results_without_detail_token_returns_none(monkeypatch):
    """Results came back but no detalleCausaCivil token could be extracted → None."""
    sc = CivilScraper(headless=True)
    page = _mock_page(["", "<tr><td>unexpected results, no token</td></tr>"])
    monkeypatch.setattr(sc, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())

    assert await sc.consulta_by_rol(MagicMock(), "C-1-2025", corte="90") is None
