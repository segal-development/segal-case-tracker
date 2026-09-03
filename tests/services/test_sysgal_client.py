"""Tests for ``SysgalClient`` — read-only HTTP connector to the Sysgal CRM."""

import json

import httpx
import pytest

from app.services.sysgal_client import SysgalClient, SysgalError

BASE = "https://sysgal.example.test"
KEY = "test-key"

FOUND_ITEM = {
    "encontrado": True,
    "id_cliente": 7,
    "rut": "123456789",
    "nombre": "PII NAME",
    "email": "pii@example.test",
    "telefono": "+56900000000",
    "estado_comercial": "Activo",
    "estado_comercial_codigo": "ACTIVO",
    "estado_comercial_color": "#22c55e",
    "tiene_contrato": True,
    "contrato": {
        "id": 1,
        "vigencia_desde": "2026-01-01",
        "vigencia_hasta": "2026-12-31",
        "fecha_creacion": "2026-01-01 10:00:00",
    },
    "updated_at": "2026-08-01 12:34:56.123456",
}
NOT_FOUND_ITEM = {
    "encontrado": False,
    "estado_comercial": None,
    "mensaje": "Cliente no encontrado",
}


def _client_with(handler) -> SysgalClient:
    transport = httpx.MockTransport(handler)
    return SysgalClient(BASE, KEY, transport=transport)


class TestConfiguration:
    def test_is_configured_true(self):
        assert SysgalClient(BASE, KEY).is_configured is True

    @pytest.mark.parametrize("base,key", [("", KEY), (BASE, ""), ("", "")])
    def test_is_configured_false(self, base, key):
        assert SysgalClient(base, key).is_configured is False


class TestBatchCap:
    def test_more_than_100_raises_value_error(self):
        client = SysgalClient(BASE, KEY)
        with pytest.raises(ValueError):
            client.estado_por_ruts([f"{i}-1" for i in range(101)])

    def test_empty_list_returns_empty_without_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"success": True, "data": {}})

        assert _client_with(handler).estado_por_ruts([]) == {}
        assert calls == []


class TestRequestShape:
    def test_uses_api_key_header_url_and_body(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"] = str(request.url)
            seen["method"] = request.method
            seen["key"] = request.headers.get("X-API-Key")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"12.345.678-9": FOUND_ITEM, "11111111-1": NOT_FOUND_ITEM},
                    "estados_posibles": {},
                },
            )

        data = _client_with(handler).estado_por_ruts(["12.345.678-9", "11111111-1"])

        assert seen["url"] == f"{BASE}/api_sync/clientes_estado"
        assert seen["method"] == "POST"
        assert seen["key"] == KEY
        assert seen["body"] == {"ruts": ["12.345.678-9", "11111111-1"]}
        # Keyed exactly as sent
        assert data["12.345.678-9"]["encontrado"] is True
        assert data["12.345.678-9"]["estado_comercial_codigo"] == "ACTIVO"
        assert data["11111111-1"]["encontrado"] is False

    def test_trailing_slash_in_base_url_is_tolerated(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"success": True, "data": {}})

        SysgalClient(BASE + "/", KEY, transport=httpx.MockTransport(handler)).estado_por_ruts(["1-9"])
        assert seen["url"] == f"{BASE}/api_sync/clientes_estado"


class TestErrors:
    def test_5xx_retried_once_then_raises(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503, text="down")

        with pytest.raises(SysgalError):
            _client_with(handler).estado_por_ruts(["1-9"])
        assert len(calls) == 2

    def test_5xx_then_200_succeeds(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json={"success": True, "data": {"1-9": NOT_FOUND_ITEM}})

        data = _client_with(handler).estado_por_ruts(["1-9"])
        assert data["1-9"]["encontrado"] is False
        assert len(calls) == 2

    def test_connection_error_retried_once_then_raises(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ConnectError("boom", request=request)

        with pytest.raises(SysgalError):
            _client_with(handler).estado_por_ruts(["1-9"])
        assert len(calls) == 2

    def test_4xx_not_retried_and_raises(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(400, json={"error": {"code": 400, "message": "x"}})

        with pytest.raises(SysgalError):
            _client_with(handler).estado_por_ruts(["1-9"])
        assert len(calls) == 1

    def test_malformed_body_raises(self):
        def handler(request):
            return httpx.Response(200, text="not json")

        with pytest.raises(SysgalError):
            _client_with(handler).estado_por_ruts(["1-9"])

    def test_missing_data_key_raises(self):
        def handler(request):
            return httpx.Response(200, json={"success": False})

        with pytest.raises(SysgalError):
            _client_with(handler).estado_por_ruts(["1-9"])

    def test_error_message_has_no_pii(self):
        def handler(request):
            return httpx.Response(500, json={"nombre": "PII NAME", "email": "pii@example.test"})

        with pytest.raises(SysgalError) as exc:
            _client_with(handler).estado_por_ruts(["1-9"])
        msg = str(exc.value)
        assert "PII NAME" not in msg
        assert "pii@example.test" not in msg
