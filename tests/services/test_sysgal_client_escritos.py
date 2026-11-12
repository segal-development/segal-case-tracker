"""Tests for the Sysgal ``escritos_estadisticas`` and ``escritos`` client methods (HTTP mocked, raw prod bodies)."""

import logging
from datetime import date

import httpx
import pytest

from app.services.sysgal_client import SysgalClient, SysgalError

BASE = "https://sysgal.example.test"
KEY = "super-secret-key"

RAW_STATS = (
    '{"success": true, "data": {"desde": "2026-10-06", "hasta": "2026-11-12", "total": 27, "por_abogado": '
    '[{"abogado_id": 272, "nombre": "Valentina Gomez Diaz", "total": 10}, {"abogado_id": 202, "nombre": '
    '"Eduardo Venegas Prado", "total": 6}, {"abogado_id": 258, "nombre": "Gonzalo Calderon Maturana", "total": 5}, '
    '{"abogado_id": 85, "nombre": "Rodrigo Campos Espinoza", "total": 4}, {"abogado_id": 93, "nombre": '
    '"Carla Lavin Benito", "total": 1}, {"abogado_id": 169, "nombre": "Pablo Acevedo Lopez", "total": 1}], '
    '"por_tipo": [{"tipo_id": "93bc9391-8b3c-4bac-9aa2-91c081f71e06", "tipo": "CURSO_PROGRESIVO_ABAN", "total": 12}, '
    '{"tipo_id": "13095c2f-884c-4218-b4bc-65f0e25cc9ac", "tipo": "EJECUTORIA_INCIDENTE_ABANDONO_DEL_PROCEDIMIENTO", '
    '"total": 8}, {"tipo_id": "b3301ecb-713f-4c06-97df-37e61285deda", "tipo": '
    '"CURSO_PROGRESIVO_Y_VISUALIZACION_DE_CAUSA", "total": 4}, {"tipo_id": "803d3270-1bca-4b77-95a9-6a7e1eba7db4", '
    '"tipo": "DESARCHIVO", "total": 2}, {"tipo_id": "2ae7fa22-3855-4d13-a4f8-0457d1662c4d", "tipo": '
    '"ABANDONO_6_MESES", "total": 1}]}}'
)
RAW_EMPTY = (
    '{"success": true, "data": {"desde": "2026-09-01", "hasta": "2026-09-30", "total": 0, '
    '"por_abogado": [], "por_tipo": []}}'
)
RAW_PAGE = (
    '{"success": true, "desde": "2026-11-01", "hasta": "2026-11-12", "data": [{"id_escrito": 3, "id_causa": 115260, '
    '"rol": "C-9061-2026", "tribunal": "19º Juzgado Civil de Santiago", "tipo_id": '
    '"93bc9391-8b3c-4bac-9aa2-91c081f71e06", "tipo": "CURSO_PROGRESIVO_ABAN", "fecha_generacion": '
    '"2026-11-03 16:53:28", "abogado_id": 93, "nombre": "Carla Lavin Benito", "origen": "autoservicio"}, '
    '{"id_escrito": 4, "id_causa": 114228, "rol": "C-2474-2023", "tribunal": "2º Juzgado de Letras de Arica", '
    '"tipo_id": "93bc9391-8b3c-4bac-9aa2-91c081f71e06", "tipo": "CURSO_PROGRESIVO_ABAN", "fecha_generacion": '
    '"2026-11-04 10:47:46", "abogado_id": 272, "nombre": "Valentina Gomez Diaz", "origen": "autoservicio"}, '
    '{"id_escrito": 5, "id_causa": 108757, "rol": "C-14450-2026", "tribunal": "8º Juzgado Civil de Santiago", '
    '"tipo_id": "b3301ecb-713f-4c06-97df-37e61285deda", "tipo": "CURSO_PROGRESIVO_Y_VISUALIZACION_DE_CAUSA", '
    '"fecha_generacion": "2026-11-04 11:01:57", "abogado_id": 272, "nombre": "Valentina Gomez Diaz", '
    '"origen": "autoservicio"}], "siguiente_desde_id": 5}'
)
D1, D2 = date(2026, 10, 6), date(2026, 11, 12)


def _client(handler) -> SysgalClient:
    return SysgalClient(BASE, KEY, transport=httpx.MockTransport(handler))


def _raw(text):
    return lambda r: httpx.Response(200, content=text, headers={"content-type": "application/json"})


class TestEscritosEstadisticas:
    def test_raw_production_body_is_unwrapped(self):
        stats = _client(_raw(RAW_STATS)).escritos_estadisticas(D1, D2)
        assert stats["total"] == 27 and stats["desde"] == "2026-10-06"
        assert len(stats["por_abogado"]) == 6 and len(stats["por_tipo"]) == 5
        assert stats["por_abogado"][0] == {"abogado_id": 272, "nombre": "Valentina Gomez Diaz", "total": 10}
        assert stats["por_tipo"][0]["tipo"] == "CURSO_PROGRESIVO_ABAN"
        assert "success" not in stats

    def test_empty_period_before_gedoc(self):
        stats = _client(_raw(RAW_EMPTY)).escritos_estadisticas(date(2026, 9, 1), date(2026, 9, 30))
        assert stats == {"desde": "2026-09-01", "hasta": "2026-09-30", "total": 0, "por_abogado": [], "por_tipo": []}

    def test_bare_form_accepted(self):
        bare = {"desde": "a", "hasta": "b", "total": 1, "por_abogado": [], "por_tipo": []}
        assert _client(lambda r: httpx.Response(200, json=bare)).escritos_estadisticas(D1, D2) == bare

    def test_sends_path_key_and_dates(self):
        seen = {}

        def handler(request):
            seen.update(path=request.url.path, key=request.headers.get("X-API-Key"), params=dict(request.url.params))
            return httpx.Response(200, content=RAW_STATS, headers={"content-type": "application/json"})

        _client(handler).escritos_estadisticas(D1, D2)
        assert seen == {
            "path": "/api_sync/escritos_estadisticas", "key": KEY,
            "params": {"desde": "2026-10-06", "hasta": "2026-11-12"},
        }

    @pytest.mark.parametrize("body", [[1], {"foo": 1}, {"por_abogado": [], "por_tipo": "x"}, {"por_abogado": "x", "por_tipo": []}])
    def test_malformed_body_is_an_error(self, body):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, json=body)).escritos_estadisticas(D1, D2)

    def test_400_never_leaks(self, caplog):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(SysgalError) as exc:
                _client(lambda r: httpx.Response(400, text="PII super-secret-key")).escritos_estadisticas(D1, D2)
        assert "400" in str(exc.value) and KEY not in str(exc.value) and "PII" not in str(exc.value)
        assert KEY not in caplog.text

    def test_5xx_retried_once_then_error(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        with pytest.raises(SysgalError):
            _client(handler).escritos_estadisticas(D1, D2)
        assert len(calls) == 2

    def test_transport_error_retried_once(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, content=RAW_EMPTY, headers={"content-type": "application/json"})

        assert _client(handler).escritos_estadisticas(D1, D2)["total"] == 0
        assert len(calls) == 2


class TestEscritos:
    def test_raw_production_page_as_received(self):
        page = _client(_raw(RAW_PAGE)).escritos(date(2026, 11, 1), D2, limite=3)
        assert page["siguiente_desde_id"] == 5 and page["desde"] == "2026-11-01"
        assert [e["id_escrito"] for e in page["data"]] == [3, 4, 5]
        assert page["data"][0]["fecha_generacion"] == "2026-11-03 16:53:28"
        assert page["data"][0]["origen"] == "autoservicio"

    def test_sends_all_params(self):
        seen = {}

        def handler(request):
            seen.update(path=request.url.path, key=request.headers.get("X-API-Key"), params=dict(request.url.params))
            return httpx.Response(200, json={"data": []})

        _client(handler).escritos(D1, D2, abogado_id=93, tipo_id="abc", desde_id=4, limite=100)
        assert seen["path"] == "/api_sync/escritos" and seen["key"] == KEY
        assert seen["params"] == {
            "desde": "2026-10-06", "hasta": "2026-11-12", "abogado_id": "93", "tipo_id": "abc",
            "desde_id": "4", "limite": "100",
        }

    def test_optional_params_omitted_default_limit_500(self):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": []})

        _client(handler).escritos(D1, D2)
        assert seen["params"] == {"desde": "2026-10-06", "hasta": "2026-11-12", "limite": "500"}

    @pytest.mark.parametrize("limite", [0, 501])
    def test_limit_out_of_range_rejected_without_request(self, limite):
        def handler(request):
            raise AssertionError("no request expected")

        with pytest.raises(ValueError):
            _client(handler).escritos(D1, D2, limite=limite)

    def test_last_page_has_null_cursor(self):
        page = _client(lambda r: httpx.Response(200, json={"success": True, "data": [], "siguiente_desde_id": None})).escritos(D1, D2)
        assert page["siguiente_desde_id"] is None

    @pytest.mark.parametrize("body", [[1], {"foo": 1}, {"data": "x"}])
    def test_body_without_data_list_is_an_error(self, body):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, json=body)).escritos(D1, D2)

    def test_http_error_never_leaks_and_5xx_retries(self, caplog):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, text="PII super-secret-key")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(SysgalError) as exc:
                _client(handler).escritos(D1, D2)
        assert len(calls) == 2
        assert KEY not in str(exc.value) and "PII" not in str(exc.value) and KEY not in caplog.text
