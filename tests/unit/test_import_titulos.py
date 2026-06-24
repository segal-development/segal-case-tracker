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


# ---------------------------------------------------------------------------
# Cascade match tests (rut, nombre, precedence)
# ---------------------------------------------------------------------------


class TestImportTitulosCascade:
    """Tests for cascade match: rol → rut → nombre."""

    def _write_csv(self, tmp_path, rows: list[dict], fieldnames=None) -> str:
        """Write rows to a temp CSV file and return path."""
        if fieldnames is None:
            fieldnames = list(rows[0].keys()) if rows else ["tipo"]
        path = str(tmp_path / "titulos_cascade.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _make_dispatch_mock(self, case_result=None, litigante_result=None):
        """Build a mock DB that returns different chains for Case vs CaseLitigante."""
        from app.models.case import Case
        from app.models.case_litigante import CaseLitigante

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)

        mock_case_q = MagicMock()
        mock_case_q.filter.return_value.all.return_value = case_result or []

        mock_lit_q = MagicMock()
        mock_lit_q.filter.return_value.all.return_value = litigante_result or []

        def _dispatch(model):
            return mock_lit_q if model is CaseLitigante else mock_case_q

        mock_db.query.side_effect = _dispatch
        return mock_db, mock_case_q, mock_lit_q

    # ------------------------------------------------------------------
    # 1. rol match
    # ------------------------------------------------------------------

    def test_rol_match_sets_tipo_and_triggers_recompute(self, tmp_path) -> None:
        """Row with rol finds that civil case, sets tipo, triggers recompute."""
        from scripts.import_titulos import import_titulos

        mock_case = MagicMock()
        mock_case.id = 1
        mock_db, mock_case_q, _ = self._make_dispatch_mock(case_result=[mock_case])

        csv_path = self._write_csv(tmp_path, [{"rol": "C-010-2026", "tipo": "pagare"}])
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            result = import_titulos(csv_path)

        assert mock_case.titulo_tipo == "pagare"
        mock_engine.recompute_case.assert_called_once_with(mock_db, mock_case)
        assert result["matched_by_rol"] == 1
        assert result["matched_by_rut"] == 0
        assert result["matched_by_name"] == 0

    # ------------------------------------------------------------------
    # 2. rut fan-out: same RUT → two cases, both updated
    # ------------------------------------------------------------------

    def test_rut_match_applies_to_all_cases_for_that_rut(self, tmp_path) -> None:
        """Row with no rol but rut applies tipo to ALL cases where that debtor RUT is a litigante."""
        from scripts.import_titulos import import_titulos
        from app.models.case import Case
        from app.models.case_litigante import CaseLitigante

        mock_case1 = MagicMock()
        mock_case1.id = 10
        mock_case2 = MagicMock()
        mock_case2.id = 20

        mock_lit1 = MagicMock()
        mock_lit1.rut = "12345678-9"
        mock_lit1.case_id = 10
        mock_lit2 = MagicMock()
        mock_lit2.rut = "12345678-9"
        mock_lit2.case_id = 20

        mock_db, mock_case_q, mock_lit_q = self._make_dispatch_mock(
            case_result=[mock_case1, mock_case2],
            litigante_result=[mock_lit1, mock_lit2],
        )

        csv_path = self._write_csv(tmp_path, [{"tipo": "cheque", "rut": "12345678-9"}])
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            result = import_titulos(csv_path)

        assert mock_case1.titulo_tipo == "cheque"
        assert mock_case2.titulo_tipo == "cheque"
        assert mock_engine.recompute_case.call_count == 2
        assert result["matched_by_rut"] == 1
        assert result["rut_cases_touched"] == 2
        assert result["matched_by_rol"] == 0

    # ------------------------------------------------------------------
    # 3. RUT normalization: dotted CSV vs plain stored
    # ------------------------------------------------------------------

    def test_rut_normalization_dotted_csv_matches_plain_stored(self, tmp_path) -> None:
        """CSV '12.345.678-9' matches stored '12345678-9' after normalize_rut on both sides."""
        from scripts.import_titulos import import_titulos, _normalize_rut_for_match
        from app.models.case import Case
        from app.models.case_litigante import CaseLitigante

        # Direct normalization unit check
        assert _normalize_rut_for_match("12.345.678-9") == _normalize_rut_for_match("12345678-9")

        mock_case = MagicMock()
        mock_case.id = 5

        mock_lit = MagicMock()
        mock_lit.rut = "12345678-9"  # stored without dots
        mock_lit.case_id = 5

        mock_db, mock_case_q, mock_lit_q = self._make_dispatch_mock(
            case_result=[mock_case],
            litigante_result=[mock_lit],
        )

        csv_path = self._write_csv(tmp_path, [{"tipo": "letra", "rut": "12.345.678-9"}])
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine"),
        ):
            result = import_titulos(csv_path)

        assert mock_case.titulo_tipo == "letra"
        assert result["matched_by_rut"] == 1

    # ------------------------------------------------------------------
    # 4. nombre match via DDO% litigante
    # ------------------------------------------------------------------

    def test_nombre_match_via_ddo_litigante(self, tmp_path) -> None:
        """Row with only nombre matches by normalized name against DDO% litigantes."""
        from scripts.import_titulos import import_titulos
        from app.models.case import Case
        from app.models.case_litigante import CaseLitigante

        mock_case = MagicMock()
        mock_case.id = 7
        mock_case.defendant = "Juan Pérez López"
        mock_case.competencia = "civil"

        mock_lit = MagicMock()
        mock_lit.nombre = "Juan Pérez López"
        mock_lit.case_id = 7
        mock_lit.participante = "DDO."

        mock_db, mock_case_q, mock_lit_q = self._make_dispatch_mock(
            case_result=[mock_case],
            litigante_result=[mock_lit],
        )

        csv_path = self._write_csv(tmp_path, [{"tipo": "pagare", "nombre": "Juan Pérez López"}])
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            result = import_titulos(csv_path)

        assert mock_case.titulo_tipo == "pagare"
        mock_engine.recompute_case.assert_called_once_with(mock_db, mock_case)
        assert result["matched_by_name"] == 1
        assert result["name_cases_touched"] == 1

    # ------------------------------------------------------------------
    # 5. unmatched row → counted, no crash
    # ------------------------------------------------------------------

    def test_unmatched_row_counted_and_no_crash(self, tmp_path) -> None:
        """Row that matches nothing is counted in unmatched without raising."""
        from scripts.import_titulos import import_titulos

        mock_db, _, _ = self._make_dispatch_mock(case_result=[], litigante_result=[])

        csv_path = self._write_csv(
            tmp_path, [{"tipo": "pagare", "rut": "99999999-9", "nombre": "Nobody"}]
        )
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine"),
        ):
            result = import_titulos(csv_path)

        assert len(result["unmatched"]) == 1
        assert result["matched_by_rol"] == 0
        assert result["matched_by_rut"] == 0
        assert result["matched_by_name"] == 0

    # ------------------------------------------------------------------
    # 6. precedence: rol wins over rut
    # ------------------------------------------------------------------

    def test_rol_takes_precedence_over_rut_no_litigante_query(self, tmp_path) -> None:
        """Row with both rol and rut uses rol (1 case); CaseLitigante is never queried."""
        from scripts.import_titulos import import_titulos
        from app.models.case_litigante import CaseLitigante

        mock_case = MagicMock()
        mock_case.id = 1
        mock_db, mock_case_q, mock_lit_q = self._make_dispatch_mock(
            case_result=[mock_case],
            litigante_result=[],
        )

        csv_path = self._write_csv(
            tmp_path,
            [{"rol": "C-010-2026", "rut": "12345678-9", "tipo": "pagare"}],
        )
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine") as mock_engine,
        ):
            result = import_titulos(csv_path)

        assert result["matched_by_rol"] == 1
        assert result["matched_by_rut"] == 0
        # CaseLitigante should NOT have been queried (rol matched → continue before rut block)
        assert not any(
            call.args[0] is CaseLitigante
            for call in mock_db.query.call_args_list
        ), "CaseLitigante must not be queried when rol matches"

    # ------------------------------------------------------------------
    # 7. fecha stored when present
    # ------------------------------------------------------------------

    def test_fecha_stored_when_present_in_csv(self, tmp_path) -> None:
        """titulo_fecha is stored when the fecha column is present and parseable."""
        from scripts.import_titulos import import_titulos

        mock_case = MagicMock()
        mock_case.id = 1

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_case]

        csv_path = self._write_csv(
            tmp_path,
            [{"rol": "C-010-2026", "tipo": "cheque", "fecha": "2024-03-15"}],
        )
        with (
            patch("scripts.import_titulos.SessionLocal", return_value=mock_db),
            patch("scripts.import_titulos.DeadlineEngine"),
        ):
            result = import_titulos(csv_path)

        assert mock_case.titulo_fecha == date(2024, 3, 15)
        assert mock_case.titulo_tipo == "cheque"
