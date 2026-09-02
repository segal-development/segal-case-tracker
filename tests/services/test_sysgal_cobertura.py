"""Tests for ``derive_cobertura`` — the pure Sysgal coverage derivation."""

from datetime import date

import pytest

from app.services.sysgal_cobertura import COBERTURAS, derive_cobertura

TODAY = date(2026, 9, 2)
PAST = date(2026, 1, 31)
FUTURE = date(2027, 6, 30)


class TestConstants:
    def test_coberturas_closed_set(self):
        assert COBERTURAS == ("activo", "moroso", "caducado", "sin_dato")


class TestSinDato:
    def test_not_found_is_sin_dato(self):
        assert derive_cobertura("ACTIVO", FUTURE, encontrado=False, today=TODAY) == "sin_dato"

    def test_none_code_is_sin_dato(self):
        assert derive_cobertura(None, FUTURE, encontrado=True, today=TODAY) == "sin_dato"

    def test_unknown_code_is_sin_dato_and_warns_once(self, monkeypatch):
        # Patch the module logger directly so the assertion does not depend on
        # global logging configuration left behind by other tests.
        import app.services.sysgal_cobertura as mod

        warned = []
        monkeypatch.setattr(mod, "_warned_codes", set())
        monkeypatch.setattr(mod.logger, "warning", lambda msg, *a, **k: warned.append(msg % a))

        assert derive_cobertura("RARO", None, encontrado=True, today=TODAY) == "sin_dato"
        assert derive_cobertura("RARO", None, encontrado=True, today=TODAY) == "sin_dato"
        assert len(warned) == 1
        assert "RARO" in warned[0]


class TestMoroso:
    def test_moroso_inactivo(self):
        assert derive_cobertura("MOROSO_INACTIVO", FUTURE, encontrado=True, today=TODAY) == "moroso"

    def test_moroso_ignores_vigencia(self):
        assert derive_cobertura("MOROSO_INACTIVO", PAST, encontrado=True, today=TODAY) == "moroso"


class TestActivo:
    def test_activo_vigente(self):
        assert derive_cobertura("ACTIVO", FUTURE, encontrado=True, today=TODAY) == "activo"

    def test_activo_without_vigencia(self):
        assert derive_cobertura("ACTIVO", None, encontrado=True, today=TODAY) == "activo"

    def test_activo_vigencia_today_is_still_activo(self):
        assert derive_cobertura("ACTIVO", TODAY, encontrado=True, today=TODAY) == "activo"

    def test_stale_activo_with_expired_vigencia_is_caducado(self):
        """Known Sysgal defect: ACTIVO code with an already-expired contract."""
        assert derive_cobertura("ACTIVO", PAST, encontrado=True, today=TODAY) == "caducado"

    def test_por_vencer_vigente(self):
        assert derive_cobertura("POR_VENCER", FUTURE, encontrado=True, today=TODAY) == "activo"

    def test_por_vencer_expired_is_caducado(self):
        assert derive_cobertura("POR_VENCER", PAST, encontrado=True, today=TODAY) == "caducado"

    def test_today_defaults_to_date_today(self):
        assert derive_cobertura("ACTIVO", date(2000, 1, 1), encontrado=True) == "caducado"


class TestCaducado:
    @pytest.mark.parametrize(
        "code", ["TERMINADO", "DESISTIDO", "ANULADO", "SIN_CONTRATO", "SIN_CONFIRMAR"]
    )
    def test_no_coverage_codes(self, code):
        assert derive_cobertura(code, FUTURE, encontrado=True, today=TODAY) == "caducado"
