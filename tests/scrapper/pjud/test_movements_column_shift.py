"""The movements parser must not depend on the HTML-commented ``<td>``.

PJUD sometimes ships a row with an HTML-commented cell (whose ``</td>`` used to
count as a phantom cell) and sometimes without it. Reading columns by fixed
position shifted description/date/foja by one in the second case.
"""

import pathlib
import re

import pytest

from app.scrapper.pjud.civil import CivilScraper
from app.scrapper.pjud.laboral import LaboralScraper
from app.scrapper.pjud.penal import PenalScraper

FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "pjud"
COMMENTED_CELL = '<!--<td><span title="Exhórtese" class="topToolNom"></span></td>-->'

SCRAPERS = [CivilScraper, LaboralScraper, PenalScraper]


def _commented_row() -> str:
    """The real fixture row (folio 40) that carries the commented cell."""
    html = (FIXTURES_DIR / "detail_civil_rich.html").read_text(encoding="utf-8")
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.DOTALL)
    row = next(r for r in rows if COMMENTED_CELL in r)
    return row


def _table(row: str) -> str:
    return (
        '<div id="historiaCiv"><table class="table table-bordered">'
        f"<tbody>{row}</tbody></table></div>"
    )


def _parse(scraper_cls, row: str):
    return scraper_cls(headless=True)._parse_movements_table(_table(row))


def _without_comment(row: str) -> str:
    assert COMMENTED_CELL in row
    return row.replace(COMMENTED_CELL, "")


@pytest.mark.parametrize("scraper_cls", SCRAPERS)
class TestCommentedCellIndependence:
    def test_with_commented_cell(self, scraper_cls):
        (m,) = _parse(scraper_cls, _commented_row())
        assert (m.folio, m.descripcion, m.fecha, m.foja) == (
            "40", "Exhórtese", "04/05/2026", "29",
        )
        assert m.etapa == "Contestación"
        assert m.tipo_tramite == "Resolución"

    def test_without_commented_cell_is_identical(self, scraper_cls):
        row = _commented_row()
        (with_c,) = _parse(scraper_cls, row)
        (without_c,) = _parse(scraper_cls, _without_comment(row))
        assert without_c == with_c

    def test_extra_cell_before_description_is_tolerated(self, scraper_cls):
        row = _without_comment(_commented_row()).replace(
            "<td> Exhórtese</td>", "<td>X</td><td> Exhórtese</td>"
        )
        (m,) = _parse(scraper_cls, row)
        assert (m.descripcion, m.fecha, m.foja) == ("Exhórtese", "04/05/2026", "29")

    def test_row_without_date_cell_has_empty_date(self, scraper_cls):
        row = _without_comment(_commented_row()).replace(
            "<td>04/05/2026</td>", "<td>sin fecha</td>"
        )
        (m,) = _parse(scraper_cls, row)
        assert m.fecha == ""
