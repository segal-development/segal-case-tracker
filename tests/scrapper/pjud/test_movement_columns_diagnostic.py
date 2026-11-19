"""A movement row without a date cell logs its raw cells, so the real layout can be seen.

Rows from some cuadernos reach sync_service with fecha='' and are skipped as corrupted
(station log 17-11-2026). Without the cells we cannot tell which layout they have.
"""
import logging

from app.scrapper.pjud.civil import locate_movement_columns


def test_row_without_date_logs_its_cells(caplog):
    cells = ["", "", "", "Apremio", "Diligencia", "EMBARGO (Exitosa) Diligencia:18/03/2026 16:36", "x"]
    with caplog.at_level(logging.WARNING, logger="app.scrapper.pjud.civil"):
        descripcion, fecha, foja = locate_movement_columns(cells)
    assert fecha == ""
    assert descripcion == "EMBARGO (Exitosa) Diligencia:18/03/2026 16:36"
    record = next(r for r in caplog.records if "movement row without date" in r.getMessage())
    assert "'Apremio'" in record.getMessage()
    assert "n_cells=7" in record.getMessage()


def test_row_with_date_logs_nothing(caplog):
    cells = ["1", "", "", "Tramitación", "Resolución", "Provee demanda", "05/11/2026", "3"]
    with caplog.at_level(logging.WARNING, logger="app.scrapper.pjud.civil"):
        assert locate_movement_columns(cells) == ("Provee demanda", "05/11/2026", "3")
    assert not [r for r in caplog.records if "movement row without date" in r.getMessage()]


def test_logged_cells_are_truncated(caplog):
    cells = ["", "", "", "", "", "A" * 500]
    with caplog.at_level(logging.WARNING, logger="app.scrapper.pjud.civil"):
        locate_movement_columns(cells)
    message = next(r for r in caplog.records if "movement row without date" in r.getMessage()).getMessage()
    assert len(message) < 600
