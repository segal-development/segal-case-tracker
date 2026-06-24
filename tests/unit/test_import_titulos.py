"""Unit tests for scripts/import_titulos.py.

Tests use unittest.mock to isolate DB session and DeadlineEngine.recompute_case.
No real DB or file I/O in most tests.
"""

from __future__ import annotations

import csv
import io
import os
import types
from datetime import date
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# _parse_fecha tests
# ---------------------------------------------------------------------------


class TestParseFecha:
    def test_parse_yyyy_mm_dd(self) -> None:
        """YYYY-MM-DD format returns correct date."""
        from scripts.import_titulos import _parse_fecha

        result = _parse_fecha("2023-05-15")
        assert result == date(2023, 5, 15)

    def test_parse_dd_mm_yyyy(self) -> None:
        """DD-MM-YYYY format returns correct date."""
        from scripts.import_titulos import _parse_fecha

        result = _parse_fecha("15-05-2023")
        assert result == date(2023, 5, 15)

    def test_unknown_format_returns_none(self) -> None:
        """Unknown format returns None."""
        from scripts.import_titulos import _parse_fecha

        result = _parse_fecha("not-a-date")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        from scripts.import_titulos import _parse_fecha

        result = _parse_fecha("")
        assert result is None


# ---------------------------------------------------------------------------
# import_titulos function tests (mock DB)
# ---------------------------------------------------------------------------


class TestImportTitulos:
    """Tests for import_titulos using mocked DB session."""

    def _write_csv(self, tmp_path, rows: list[dict], fieldnames=None) -> str:
        """Write rows to a temp CSV file and return path."""
        path = str(tmp_path / "titulos.csv")
        if fieldnames is None:
            fieldnames = ["rol", "tipo", "fecha"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_rol_tipo_only_csv_maps_tipo_and_recomputes(self, tmp_path) -> None:
        """A CSV with only rol,tipo (no fecha column) maps tipo + triggers recompute."""
        from scripts.import_titulos import import_titulos

        mock_case = MagicMock()

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_case]

        csv_path = self._write_csv(
            tmp_path,
            [{"rol": "C-010-2026", "tipo": "pagare"}],
            fieldnames=["rol", "tipo"],
        )

        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            result = import_titulos(csv_path)

        assert mock_case.titulo_tipo == "pagare"
        assert mock_case.titulo_fecha is None  # no fecha column → None
        mock_engine.recompute_case.assert_called_once_with(mock_db, mock_case)
        assert result["updated"] == 1

    def test_tipo_stored_lowercased(self, tmp_path) -> None:
        """Unknown tipo stored lowercased: 'PAGARE' → 'pagare'."""
        from scripts.import_titulos import import_titulos

        mock_case = MagicMock()
        mock_case.titulo_tipo = None
        mock_case.titulo_fecha = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_case]

        csv_path = self._write_csv(tmp_path, [{"rol": "C-001-2026", "tipo": "PAGARE", "fecha": "2023-01-01"}])

        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            result = import_titulos(csv_path)

        assert mock_case.titulo_tipo == "pagare"

    def test_matched_case_gets_titulo_tipo_and_fecha_set(self, tmp_path) -> None:
        """Matched case gets titulo_tipo and titulo_fecha set from CSV."""
        from scripts.import_titulos import import_titulos

        mock_case = MagicMock()

        mock_db = MagicMock()
        # First query (civil filter) returns our case
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_case]

        csv_path = self._write_csv(tmp_path, [{"rol": "C-001-2026", "tipo": "cheque", "fecha": "2023-06-15"}])

        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            result = import_titulos(csv_path)

        assert mock_case.titulo_tipo == "cheque"
        assert mock_case.titulo_fecha == date(2023, 6, 15)

    def test_recompute_case_called_on_matched_cases(self, tmp_path) -> None:
        """DeadlineEngine.recompute_case is called for each matched case."""
        from scripts.import_titulos import import_titulos

        mock_case1 = MagicMock()
        mock_case2 = MagicMock()

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_case1, mock_case2]

        csv_path = self._write_csv(tmp_path, [{"rol": "C-002-2026", "tipo": "pagare", "fecha": "2022-01-01"}])

        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            result = import_titulos(csv_path)

        assert mock_engine.recompute_case.call_count == 2
        mock_engine.recompute_case.assert_any_call(mock_db, mock_case1)
        mock_engine.recompute_case.assert_any_call(mock_db, mock_case2)

    def test_unmatched_rols_recorded_in_not_found(self, tmp_path) -> None:
        """Rols that don't match any case are recorded in not_found list."""
        from scripts.import_titulos import import_titulos

        mock_db = MagicMock()
        # Both queries (civil + all) return empty list → not found
        mock_db.query.return_value.filter.return_value.all.return_value = []

        csv_path = self._write_csv(tmp_path, [{"rol": "X-999-2026", "tipo": "pagare", "fecha": "2022-01-01"}])

        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine"),
        ):
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            result = import_titulos(csv_path)

        assert "X-999-2026" in result["not_found"]
        assert result["matched"] == 0

    def test_matched_count_returned_correctly(self, tmp_path) -> None:
        """Summary dict has correct matched/updated counts."""
        from scripts.import_titulos import import_titulos

        mock_case = MagicMock()

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_case]

        csv_path = self._write_csv(tmp_path, [
            {"rol": "C-001-2026", "tipo": "pagare", "fecha": "2022-01-01"},
        ])

        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine"),
        ):
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            result = import_titulos(csv_path)

        assert result["matched"] == 1
        assert result["updated"] == 1
        assert result["not_found"] == []
