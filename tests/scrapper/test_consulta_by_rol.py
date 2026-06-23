"""Unit tests for CivilScraper.consulta_by_rol — the consulta-by-rol scraping mode.

The method is also validated live against PJUD; these tests guard the branching
logic (reserved/not-found → None, found → parsed via the existing detail parser)
and the multi-cuaderno accumulation/dedup, without touching the network.
asyncio_mode=auto (no marker needed).
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
    detail = MagicMock()
    detail.movements = []
    # evaluate calls: consultaUnificada, search, detalleCausaCivil, read modal,
    # then _load_other_cuadernos reads the selCuaderno values (1 → early return).
    page = _mock_page(["", search_html, "", "<div id='historiaCiv'></div>", ["cuad1"]])
    monkeypatch.setattr(sc, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(sc, "_parse_case_detail_html", MagicMock(return_value=detail))

    result = await sc.consulta_by_rol(MagicMock(), "C-7545-2025", corte="90")

    assert result is detail
    # the JWT extracted from the search HTML is passed as the case token
    assert sc._parse_case_detail_html.call_args.args[1] == "JWT123"


async def test_results_without_detail_token_returns_none(monkeypatch):
    """Results came back but no detalleCausaCivil token could be extracted → None."""
    sc = CivilScraper(headless=True)
    page = _mock_page(["", "<tr><td>unexpected results, no token</td></tr>"])
    monkeypatch.setattr(sc, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())

    assert await sc.consulta_by_rol(MagicMock(), "C-1-2025", corte="90") is None


async def test_load_other_cuadernos_accumulates_and_dedups(monkeypatch):
    """Non-default cuadernos (e.g. Apremio) are loaded; new movements merge, dups skip."""
    sc = CivilScraper(headless=True)
    page = AsyncMock()
    # 1st evaluate → selCuaderno values (2 cuadernos); 2nd → the modal HTML for cuad B
    page.evaluate = AsyncMock(side_effect=[["cuadA", "cuadB"], "<cuadB html>"])
    monkeypatch.setattr("app.scrapper.pjud.civil.asyncio.sleep", AsyncMock())

    detail = MagicMock()
    detail.movements = [MagicMock(folio="1", descripcion="A")]  # cuaderno A already parsed
    # cuaderno B yields a duplicate (1/A) and a genuinely new one (1/B)
    cuad_b = [MagicMock(folio="1", descripcion="A"), MagicMock(folio="1", descripcion="B")]
    monkeypatch.setattr(sc, "_parse_movements_table", MagicMock(return_value=cuad_b))

    await sc._load_other_cuadernos(page, detail)

    keys = {(m.folio, m.descripcion) for m in detail.movements}
    assert keys == {("1", "A"), ("1", "B")}
    assert len(detail.movements) == 2  # the (1, A) duplicate was not re-added
    page.select_option.assert_awaited_once_with("#selCuaderno", "cuadB")


async def test_load_other_cuadernos_single_cuaderno_noop(monkeypatch):
    """A single-cuaderno case loads nothing extra."""
    sc = CivilScraper(headless=True)
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=[["onlyone"]])
    detail = MagicMock()
    detail.movements = [MagicMock(folio="1", descripcion="A")]

    await sc._load_other_cuadernos(page, detail)

    assert len(detail.movements) == 1
    page.select_option.assert_not_awaited()
