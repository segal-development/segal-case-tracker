"""``SysgalClient.cuotas_contratos`` — GET /api_sync/cuotas (all HTTP mocked)."""

import logging
from datetime import datetime

import httpx
import pytest

from app.services.sysgal_client import MAX_CUOTAS_PAGES, SysgalClient, SysgalError

BASE = "https://sysgal.example.test"
KEY = "super-secret-key"


def _client(handler) -> SysgalClient:
    return SysgalClient(BASE, KEY, transport=httpx.MockTransport(handler))


def _row(contrato, cuota=1, moroso=True, causas=(10,), **extra):
    row = {
        "id": 1, "contrato": contrato, "rut": "11.111.111-1", "cliente": "PII NAME",
        "cuota": cuota, "monto": 100, "cobrador": {"id": 1, "nombre": "PII"},
        "abogado": {"id": 2, "nombre": "PII"},
        "contrato_moroso": moroso, "contrato_al_dia": not moroso, "cuota_actual": cuota,
        "id_causa": causas[0] if causas else None, "id_causas": list(causas),
    }
    row.update(extra)
    return row


def _body(rows, nxt=None, **extra):
    body = {
        "success": True, "encontrado": True, "actualizado": "2026-11-18 07:06:47",
        "actualizacion": "diaria (~07:00)", "data": rows,
    }
    if nxt is not None:
        body["siguiente_desde_id"] = nxt
    body.update(extra)
    return body


def test_single_page_unwraps_envelope_and_minimizes():
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = request.url, request.headers.get("X-API-Key")
        return httpx.Response(200, json=_body([_row(7, cuota=2, causas=(10, 11))]))

    res = _client(handler).cuotas_contratos()
    assert seen["url"].path == "/api_sync/cuotas"
    assert seen["key"] == KEY
    assert res.encontrado is True
    assert res.copia_at == datetime(2026, 11, 18, 7, 6, 47)
    (c,) = res.contratos
    assert (c.contrato, c.cuota_actual, c.contrato_moroso, c.contrato_al_dia) == ("7", 2, True, False)
    assert c.id_causas == [10, 11]
    assert not hasattr(c, "cliente") and not hasattr(c, "rut")


def test_paginates_until_cursor_exhausted_and_merges_cuota_rows():
    cursors = []

    def handler(request):
        cursor = int(request.url.params["desde_id"])
        cursors.append(cursor)
        if cursor == 0:
            return httpx.Response(200, json=_body([_row(1), _row(1)], nxt=50))
        if cursor == 50:
            return httpx.Response(200, json=_body([_row(1), _row(2, causas=())], nxt=90))
        return httpx.Response(200, json=_body([_row(3, moroso=False, cuota=5)]))

    res = _client(handler).cuotas_contratos()
    assert cursors == [0, 50, 90]
    assert [c.contrato for c in res.contratos] == ["1", "2", "3"]
    assert res.contratos[1].id_causas == []


def test_id_causa_without_id_causas_is_kept():
    row = _row(1)
    row.pop("id_causas")
    row["id_causa"] = 33
    res = _client(lambda r: httpx.Response(200, json=_body([row]))).cuotas_contratos()
    assert res.contratos[0].id_causas == [33]


def test_encontrado_false_means_no_information():
    body = {"success": True, "encontrado": False, "data": []}
    res = _client(lambda r: httpx.Response(200, json=body)).cuotas_contratos()
    assert res.encontrado is False
    assert res.contratos == [] and res.copia_at is None


def test_copia_at_comes_from_actualizado_never_from_actualizacion():
    # real envelope (probed 18-11): actualizacion is a description, not a date
    res = _client(lambda r: httpx.Response(200, json=_body([_row(1)]))).cuotas_contratos()
    assert res.copia_at == datetime(2026, 11, 18, 7, 6, 47)


def test_copia_at_falls_back_to_rows_actualizado():
    body = _body([_row(1, actualizado="2026-11-18 07:06:47")])
    body.pop("actualizado")
    res = _client(lambda r: httpx.Response(200, json=body)).cuotas_contratos()
    assert res.copia_at == datetime(2026, 11, 18, 7, 6, 47)


def test_copia_at_none_when_no_date_anywhere():
    body = _body([_row(1)])
    body.pop("actualizado")
    res = _client(lambda r: httpx.Response(200, json=body)).cuotas_contratos()
    assert res.copia_at is None


@pytest.mark.parametrize("bad", ["x", 5, None, {"cuota": 1}, {"contrato": None}, {"contrato": " "}])
def test_malformed_item_fails_the_whole_sync(bad):
    def handler(request):
        if request.url.params["desde_id"] == "0":
            return httpx.Response(200, json=_body([_row(1)], nxt=5))
        return httpx.Response(200, json=_body([_row(2), bad]))

    with pytest.raises(SysgalError):
        _client(handler).cuotas_contratos()


def test_inconsistent_rows_of_one_contract_never_block(caplog):
    rows = [_row(1, cuota=1, moroso=True), _row(1, cuota=1, moroso=False), _row(2, cuota=2, moroso=True)]
    with caplog.at_level(logging.WARNING):
        res = _client(lambda r: httpx.Response(200, json=_body(rows))).cuotas_contratos()
    by = {c.contrato: c for c in res.contratos}
    assert by["1"].contrato_moroso is False
    assert by["2"].contrato_moroso is True
    assert "1 contract" in caplog.text and "PII" not in caplog.text


def test_consistent_rows_keep_first_row_values():
    rows = [_row(1, cuota=2, moroso=True), _row(1, cuota=2, moroso=True)]
    res = _client(lambda r: httpx.Response(200, json=_body(rows))).cuotas_contratos()
    assert res.contratos[0].contrato_moroso is True and res.contratos[0].cuota_actual == 2


def test_failure_mid_pagination_raises_without_partial_result():
    def handler(request):
        if request.url.params["desde_id"] == "0":
            return httpx.Response(200, json=_body([_row(1)], nxt=5))
        return httpx.Response(403)

    with pytest.raises(SysgalError):
        _client(handler).cuotas_contratos()


def test_5xx_retried_once_then_error_without_key_or_body(caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502, text="SECRET BODY")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SysgalError) as exc:
            _client(handler).cuotas_contratos()
    assert len(calls) == 2
    assert KEY not in str(exc.value) and "SECRET" not in str(exc.value)
    assert KEY not in caplog.text and "SECRET" not in caplog.text


def test_stuck_cursor_and_missing_data_raise():
    stuck = lambda r: httpx.Response(200, json=_body([_row(1)], nxt=0))  # noqa: E731
    with pytest.raises(SysgalError):
        _client(stuck).cuotas_contratos()
    with pytest.raises(SysgalError):
        _client(lambda r: httpx.Response(200, json={"success": True, "encontrado": True})).cuotas_contratos()


def test_page_cap_constant():
    assert MAX_CUOTAS_PAGES >= 100
