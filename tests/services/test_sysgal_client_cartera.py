"""Tests for the Sysgal ``estadisticas_cartera`` and ``causas_sin_gestion`` client methods (HTTP mocked)."""

import logging
from datetime import date

import httpx
import pytest

from app.services.sysgal_client import SysgalClient, SysgalError

BASE = "https://sysgal.example.test"
KEY = "super-secret-key"

STATS = {
    "desde": "2026-11-01",
    "hasta": "2026-11-10",
    "total_activas": 12584,
    "total_morosas": 38593,
    "gestionadas": 5243,
    "sin_gestion": 7341,
    "cobertura": 41.66,
    "por_matriz": [{"matriz": "M1 Baja", "total": 7471, "gestionadas": 3171, "cobertura": 42.44}],
    "por_abogado": [{"abogado_id": 238, "nombre": "Luis C", "total": 1057, "gestionadas": 341, "cobertura": 32.26}],
}


def _client(handler) -> SysgalClient:
    return SysgalClient(BASE, KEY, transport=httpx.MockTransport(handler))


class TestEstadisticasCartera:
    def test_real_envelope_success_data_is_unwrapped(self):
        # Production (2026-11-11) answers {"success": true, "data": {...stats...}}.
        handler = lambda r: httpx.Response(200, json={"success": True, "data": STATS})  # noqa: E731

        assert _client(handler).estadisticas_cartera(date(2026, 11, 1), date(2026, 11, 10)) == STATS

    def test_sends_path_key_and_dates_and_returns_payload_as_received(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("X-API-Key")
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=STATS)

        body = _client(handler).estadisticas_cartera(date(2026, 11, 1), date(2026, 11, 10))

        assert seen["path"] == "/api_sync/estadisticas_cartera"
        assert seen["key"] == KEY
        assert seen["params"] == {"desde": "2026-11-01", "hasta": "2026-11-10"}
        assert body == STATS

    def test_400_raises_without_key_or_body(self, caplog):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(SysgalError) as exc:
                _client(lambda r: httpx.Response(400, text="PII BODY super-secret-key")).estadisticas_cartera(
                    date(2026, 1, 1), date(2026, 11, 10)
                )
        assert "400" in str(exc.value)
        assert KEY not in str(exc.value) and "PII BODY" not in str(exc.value)
        assert KEY not in caplog.text

    def test_5xx_retried_once_then_error(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        with pytest.raises(SysgalError):
            _client(handler).estadisticas_cartera(date(2026, 11, 1), date(2026, 11, 10))
        assert len(calls) == 2

    def test_transport_error_retried_once(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json=STATS)

        assert _client(handler).estadisticas_cartera(date(2026, 11, 1), date(2026, 11, 10)) == STATS
        assert len(calls) == 2

    def test_non_json_twice_is_an_error(self):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, text="<html>")).estadisticas_cartera(
                date(2026, 11, 1), date(2026, 11, 10)
            )

    @pytest.mark.parametrize("body", [[1, 2], {"foo": 1}, {"por_abogado": "x"}])
    def test_body_without_por_abogado_list_is_an_error(self, body):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, json=body)).estadisticas_cartera(
                date(2026, 11, 1), date(2026, 11, 10)
            )


class TestCausasSinGestion:
    PAGE = {
        "success": True,
        "desde": "2026-11-01",
        "hasta": "2026-11-10",
        "data": [{"id_causa": 1210, "rol": "C-4152-2018", "tribunal": "1º JC", "abogado_id": 83,
                  "ultima_gestion": "2026-10-28 13:31:36"}],
        "siguiente_desde_id": 1703,
    }

    def test_sends_all_params_and_returns_page(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=self.PAGE)

        page = _client(handler).causas_sin_gestion(
            date(2026, 11, 1), date(2026, 11, 10), abogado_id=83, desde_id=1200, limite=100
        )

        assert seen["path"] == "/api_sync/causas_sin_gestion"
        assert seen["params"] == {
            "desde": "2026-11-01", "hasta": "2026-11-10", "abogado_id": "83", "desde_id": "1200", "limite": "100",
        }
        assert page == self.PAGE

    def test_optional_params_omitted_by_default(self):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": []})

        _client(handler).causas_sin_gestion(date(2026, 11, 1), date(2026, 11, 10))

        assert seen["params"] == {"desde": "2026-11-01", "hasta": "2026-11-10", "limite": "500"}

    @pytest.mark.parametrize("limite", [0, 501])
    def test_limit_out_of_range_rejected_without_request(self, limite):
        def handler(request):
            raise AssertionError("no request expected")

        with pytest.raises(ValueError):
            _client(handler).causas_sin_gestion(date(2026, 11, 1), date(2026, 11, 10), limite=limite)

    def test_http_error_never_leaks(self, caplog):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(SysgalError) as exc:
                _client(lambda r: httpx.Response(403, text="PII super-secret-key")).causas_sin_gestion(
                    date(2026, 11, 1), date(2026, 11, 10)
                )
        assert KEY not in str(exc.value) and "PII" not in str(exc.value)
        assert KEY not in caplog.text

    def test_body_without_data_list_is_an_error(self):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, json={"foo": 1})).causas_sin_gestion(
                date(2026, 11, 1), date(2026, 11, 10)
            )
