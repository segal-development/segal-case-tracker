"""Receptor actuations carry TWO dates in the date cell: ``27/05/2025 (20/05/2025)``.

The first is the movement date shown by PJUD; the parenthesised one is the
diligence date. The row must keep the first as fecha instead of being dropped
as corrupted (station log 23-11-2026).
"""
import logging
import pathlib
import re

from app.scrapper.pjud.civil import CivilScraper, locate_movement_columns

FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "pjud"
COMMENTED_CELL = '<!--<td><span title="Exhórtese" class="topToolNom"></span></td>-->'


def test_same_date_twice_with_double_space():
    cells = ['11', 'Documento principal del escrito', '', 'Excepciones', 'Actuación Receptor',
             'Certificación', '31/05/2025  (31/05/2025)', '0', '', '']
    assert locate_movement_columns(cells) == ("Certificación", "31/05/2025", "0")


def test_different_dates_keep_the_first():
    cells = ['8', 'Documento principal del escrito', '', 'Notificación demanda y su proveído',
             'Actuación Receptor', 'NOTIFICACIÓN  DE DEMANDA', '27/05/2025 (20/05/2025)', '0', '', '']
    assert locate_movement_columns(cells) == ("NOTIFICACIÓN  DE DEMANDA", "27/05/2025", "0")


def test_no_whitespace_before_parenthesis():
    cells = ["1", "", "", "Etapa", "Tramite", "Desc", "27/05/2025(20/05/2025)", "2"]
    assert locate_movement_columns(cells) == ("Desc", "27/05/2025", "2")


def test_normal_row_unchanged(caplog):
    cells = ["1", "", "", "Tramitación", "Resolución", "Provee demanda", "05/11/2026", "3"]
    with caplog.at_level(logging.WARNING, logger="app.scrapper.pjud.civil"):
        assert locate_movement_columns(cells) == ("Provee demanda", "05/11/2026", "3")
    assert not caplog.records


def test_date_inside_description_is_not_the_date_cell(caplog):
    cells = ["", "", "", "Apremio", "Diligencia",
             "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:17/06/2025 09:29", "x"]
    with caplog.at_level(logging.WARNING, logger="app.scrapper.pjud.civil"):
        _, fecha, _ = locate_movement_columns(cells)
    assert fecha == ""
    assert any("movement row without date" in r.getMessage() for r in caplog.records)


def test_date_followed_by_text_is_not_a_date_cell():
    cells = ["", "", "", "E", "T", "Desc", "17/06/2025 09:29", "x"]
    assert locate_movement_columns(cells)[1] == ""


def test_only_parenthesised_date_is_not_a_movement_date(caplog):
    cells = ["", "", "", "E", "T", "Desc", "(20/05/2025)", "0"]
    with caplog.at_level(logging.WARNING, logger="app.scrapper.pjud.civil"):
        _, fecha, _ = locate_movement_columns(cells)
    assert fecha == ""
    assert any("movement row without date" in r.getMessage() for r in caplog.records)


def test_parse_movements_table_keeps_double_date_row():
    html = (FIXTURES_DIR / "detail_civil_rich.html").read_text(encoding="utf-8")
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.DOTALL)
    row = next(r for r in rows if COMMENTED_CELL in r)
    row = row.replace(COMMENTED_CELL, "").replace("<td>04/05/2026</td>", "<td>04/05/2026 (28/04/2026)</td>")
    assert "(28/04/2026)" in row
    table = f'<div id="historiaCiv"><table class="table"><tbody>{row}</tbody></table></div>'
    (m,) = CivilScraper(headless=True)._parse_movements_table(table)
    assert (m.descripcion, m.fecha, m.foja) == ("Exhórtese", "04/05/2026", "29")
