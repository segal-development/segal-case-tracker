"""Tests for the Sysgal ``/api_sync/causas`` and ``/api_sync/usuarios`` client methods (all HTTP mocked)."""

import logging
from datetime import datetime

import httpx
import pytest

from app.services.sysgal_client import (
    MAX_CAUSAS_PAGES,
    MAX_CAUSAS_PER_PAGE,
    SysgalClient,
    SysgalError,
    map_causa,
    map_usuario,
)

BASE = "https://sysgal.example.test"
KEY = "super-secret-key"


def _client(handler) -> SysgalClient:
    return SysgalClient(BASE, KEY, transport=httpx.MockTransport(handler))


def _raw(id_causa, **extra):
    return {"id_causa": id_causa, "rol": "C-1-2026", "tribunal": "1º Juzgado Civil", **extra}


class TestCausasPage:
    def test_sends_key_header_path_and_params(self):
        seen = {}

        def handler(request):
            seen["url"] = request.url
            seen["key"] = request.headers.get("X-API-Key")
            return httpx.Response(200, json={"data": [_raw(1)], "siguiente_desde_id": None})

        page = _client(handler).causas(
            desde_id=5, limite=100, actualizado_desde=datetime(2026, 11, 1, 10, 30), solo_activas=True
        )

        assert seen["url"].path == "/api_sync/causas"
        assert seen["key"] == KEY
        params = dict(seen["url"].params)
        assert params["desde_id"] == "5"
        assert params["limite"] == "100"
        assert params["actualizado_desde"] == "2026-11-01 10:30:00"
        assert params["solo_activas"] == "1"  # Sysgal docs: 0 or 1
        assert [c.sysgal_id for c in page["items"]] == [1]
        assert page["siguiente_desde_id"] is None

    def test_optional_filters_are_omitted_when_not_given(self):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": []})

        _client(handler).causas()

        assert seen["params"] == {"desde_id": "0", "limite": "500"}

    @pytest.mark.parametrize("limite", [0, MAX_CAUSAS_PER_PAGE + 1])
    def test_limit_out_of_range_rejected_without_request(self, limite):
        def handler(request):
            raise AssertionError("no request expected")

        with pytest.raises(ValueError):
            _client(handler).causas(limite=limite)

    def test_http_error_raises_without_leaking_key_or_body(self, caplog):
        def handler(request):
            return httpx.Response(403, text="PII BODY super-secret-key")

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(SysgalError) as exc:
                _client(handler).causas()

        assert "403" in str(exc.value)
        assert KEY not in str(exc.value) and "PII BODY" not in str(exc.value)
        assert KEY not in caplog.text

    def test_5xx_retried_once_then_error(self, caplog):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(SysgalError):
                _client(handler).causas()

        assert len(calls) == 2
        assert KEY not in caplog.text

    def test_transport_error_retried_once(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"data": [_raw(1)]})

        assert len(_client(handler).causas()["items"]) == 1
        assert len(calls) == 2

    def test_non_json_body_is_an_error(self):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, text="<html>")).causas()

    def test_body_without_item_list_is_an_error(self):
        with pytest.raises(SysgalError):
            _client(lambda r: httpx.Response(200, json={"foo": 1})).causas()


class TestIterCausas:
    def test_follows_siguiente_desde_id_until_null(self):
        pages = {
            "0": {"data": [_raw(1), _raw(2)], "siguiente_desde_id": 2},
            "2": {"data": [_raw(3)], "siguiente_desde_id": None},
        }
        asked = []

        def handler(request):
            desde = request.url.params["desde_id"]
            asked.append(desde)
            return httpx.Response(200, json=pages[desde])

        ids = [c.sysgal_id for c in _client(handler).iter_causas()]

        assert ids == [1, 2, 3]
        assert asked == ["0", "2"]

    def test_stops_when_siguiente_desde_id_absent(self):
        def handler(request):
            return httpx.Response(200, json={"data": [_raw(1)]})

        assert [c.sysgal_id for c in _client(handler).iter_causas()] == [1]

    def test_passes_filters_on_every_page(self):
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            nxt = 1 if request.url.params["desde_id"] == "0" else None
            return httpx.Response(200, json={"data": [_raw(1)], "siguiente_desde_id": nxt})

        list(_client(handler).iter_causas(actualizado_desde="2026-11-01 00:00:00", solo_activas=True))

        assert len(seen) == 2
        assert all(p["actualizado_desde"] == "2026-11-01 00:00:00" and p["solo_activas"] == "1" for p in seen)

    def test_page_cap_stops_a_cursor_that_never_ends(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"data": [_raw(len(calls))], "siguiente_desde_id": len(calls)})

        with pytest.raises(SysgalError):
            list(_client(handler).iter_causas())

        assert len(calls) == MAX_CAUSAS_PAGES

    def test_non_advancing_cursor_is_an_error(self):
        def handler(request):
            return httpx.Response(200, json={"data": [_raw(1)], "siguiente_desde_id": 0})

        with pytest.raises(SysgalError):
            list(_client(handler).iter_causas())


class TestUsuarios:
    def test_returns_mapped_list(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("X-API-Key")
            return httpx.Response(200, json={"data": [{"id": 4, "nombre": "Ana", "perfil": "Abogado", "estado": "activo"}]})

        users = _client(handler).usuarios()

        assert seen == {"path": "/api_sync/usuarios", "key": KEY}
        assert [(u.sysgal_id, u.nombre) for u in users] == [(4, "Ana")]


class TestFieldMapping:
    def test_maps_required_fields(self):
        c = map_causa({
            "id_causa": 10, "rol": "C-9-2026", "tribunal": "2º Juzgado Civil de Santiago",
            "rut_cliente": "12.345.678-5", "abogado_id": 7, "abogado_nombre": "Ana Pérez",
            "etapa": "Prueba", "tramite": "Notificación", "fecha_actualizacion": "2026-11-02 08:00:00",
        })
        assert (c.sysgal_id, c.rol, c.tribunal) == (10, "C-9-2026", "2º Juzgado Civil de Santiago")
        assert (c.rut_cliente, c.abogado_sysgal_id, c.abogado_nombre) == ("12.345.678-5", 7, "Ana Pérez")
        assert (c.etapa, c.tramite) == ("Prueba", "Notificación")
        assert c.updated_at == datetime(2026, 11, 2, 8, 0, 0)
        assert c.eliminada is False and c.tribunal_codigo is None and c.estado is None

    def test_maps_optional_extras_and_ignores_unknown_fields(self):
        c = map_causa({
            "id_causa": 10, "rol": "C-9-2026", "tribunal": "T", "tribunal_codigo": "1234",
            "estado": "Activa", "competencia": "civil", "eliminada": 1,
            "responsable_desde": "2026-10-01", "campo_nuevo": {"x": 1},
        })
        assert (c.tribunal_codigo, c.estado, c.competencia, c.eliminada) == ("1234", "Activa", "civil", True)
        assert str(c.responsable_desde) == "2026-10-01"

    def test_missing_id_causa_is_an_error(self):
        with pytest.raises(SysgalError):
            map_causa({"rol": "C-1-2026"})

    def test_usuario_mapping_with_extras(self):
        u = map_usuario({"id": 1, "nombre": "Ana", "perfil": "Abogado", "estado": "activo", "correo": "a@x.cl", "zzz": 1})
        assert (u.sysgal_id, u.nombre, u.perfil, u.estado, u.correo) == (1, "Ana", "Abogado", "activo", "a@x.cl")
        assert map_usuario({"id": 2, "nombre": "B"}).correo is None


class TestRealSysgalShape:
    """Shapes from the real endpoints (probed from the QA VM, 2026-11-09)."""

    REAL_CAUSA = {
        "id_causa": 279, "rol": "C-1234-2025", "tribunal": "7º Juzgado Civil de Santiago",
        "rut_cliente": "12.345.678-5", "abogado": {"id": 41, "nombre": "BENJAMIN HERVAS"},
        "abogados": [{"id": 41, "nombre": "BENJAMIN HERVAS"}, {"id": 3, "nombre": "CARLA LAVIN"}],
        "etapa": None, "tramite": None, "activa": True, "fecha_actualizacion": "2026-11-08 17:05:33",
    }

    def test_maps_the_abogado_object_and_activa(self):
        dto = map_causa(self.REAL_CAUSA)
        assert (dto.sysgal_id, dto.abogado_sysgal_id, dto.abogado_nombre) == (279, 41, "BENJAMIN HERVAS")
        assert dto.estado == "activa"
        assert dto.updated_at == datetime(2026, 11, 8, 17, 5, 33)
        assert dto.etapa is None and dto.tramite is None

    def test_inactive_and_null_abogado(self):
        dto = map_causa({**self.REAL_CAUSA, "abogado": None, "abogados": [], "activa": False})
        assert dto.abogado_sysgal_id is None and dto.abogado_nombre is None
        assert dto.estado == "terminada"

    def test_maps_the_real_usuario(self):
        u = map_usuario({"id": 41, "nombre": "BENJAMIN HERVAS", "tipo": 3, "tipo_nombre": "Abogado", "activo": True})
        assert (u.sysgal_id, u.nombre, u.perfil, u.estado, u.correo) == (41, "BENJAMIN HERVAS", "Abogado", "activo", None)
        assert map_usuario({"id": 7, "nombre": "X", "tipo": 4, "tipo_nombre": "Procurador", "activo": False}).estado == "inactivo"

    def test_solo_activas_is_sent_as_1_or_0(self):
        client, calls = _client_recording()
        client.causas(solo_activas=True)
        client.causas(solo_activas=False)
        assert [c["solo_activas"] for c in calls] == ["1", "0"]


def _client_recording():
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"success": True, "data": [], "siguiente_desde_id": None})

    return _client(handler), calls
