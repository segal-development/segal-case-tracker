"""One "causa activa" for every total: each endpoint's total equals the es_activa count within its scope."""
import pytest

from tests.api.v1.procurador_support import (
    headers,
    link,
    make_case,
    make_court,
    make_lawyer,
)

DEFINICION = "no terminada en Sysgal; contrato vigente, moroso o sin dato"
EXCLUIDAS = {"terminada_sysgal": 1, "contrato_caducado": 1}


@pytest.fixture
def world(db):
    court = make_court(db)
    abogado = make_lawyer(db, "11111111-1", "Abogado A", nivel="junior")
    admin = make_lawyer(db, "22222222-2", "Admin", role="admin")
    proc = make_lawyer(db, "33333333-3", "Proc", role="procurador")
    link(db, proc, abogado)
    cases = {}
    for key, fields in {
        "activo": {"contrato_estado": "activo"},
        "moroso": {"contrato_estado": "moroso"},
        "sin_dato": {},
        "caducado": {"contrato_estado": "caducado"},
        "terminada": {"terminada_en_sysgal": True, "contrato_estado": "activo"},
    }.items():
        case = make_case(db, abogado, court, f"C-{len(cases) + 1}-2026", abogado)
        case.matriz = "M3"
        case.matriz_origen = "pjud_etapa"
        for k, v in fields.items():
            setattr(case, k, v)
        cases[key] = case
    db.commit()
    return {"abogado": abogado, "admin": admin, "proc": proc, "cases": cases}


def _ruts(w):
    return {"lawyer": w["abogado"].rut, "admin": w["admin"].rut, "procurador": w["proc"].rut}


ENDPOINTS = [
    ("/api/v1/cases", lambda b: b["total"], ["lawyer", "admin", "procurador"]),
    ("/api/v1/cases/summary", lambda b: b["total"], ["lawyer", "admin", "procurador"]),
    ("/api/v1/matriz/distribucion", lambda b: b["total"], ["lawyer", "admin"]),
    ("/api/v1/stats/me", lambda b: b["case_count"], ["lawyer", "procurador"]),
    ("/api/v1/stats/firm", lambda b: b["totals"]["cases"], ["lawyer", "admin"]),
    ("/api/v1/stats/risk-board", lambda b: b["total"], ["admin"]),
]


@pytest.mark.parametrize(
    "path,total,role",
    [(path, total, role) for path, total, roles in ENDPOINTS for role in roles],
)
def test_total_is_the_count_of_activas_and_reports_what_is_left_out(client, world, path, total, role):
    resp = client.get(path, headers=headers(_ruts(world)[role]))

    assert resp.status_code == 200
    body = resp.json()
    assert total(body) == 3
    assert body["activas_definicion"] == DEFINICION
    assert body["excluidas"] == EXCLUIDAS


def test_por_abogado_counts_activas_only(client, world):
    body = client.get("/api/v1/matriz/por-abogado", headers=headers(world["admin"].rut)).json()
    mine = next(i for i in body["items"] if i["lawyer_id"] == world["abogado"].id)
    assert mine["total"] == 3
    assert body["activas_definicion"] == DEFINICION
    assert body["excluidas"] == EXCLUIDAS


def test_desajustes_list_only_activas_and_report_the_rest(client, world):
    # M3 needs a senior; the abogado is junior: every case is a desajuste.
    body = client.get("/api/v1/asignacion/desajustes", headers=headers(world["admin"].rut)).json()
    ids = {i["case_id"] for i in body["items"]}
    expected = {world["cases"][k].id for k in ("activo", "moroso", "sin_dato")}
    assert ids == expected and body["total"] == 3
    assert body["activas_definicion"] == DEFINICION
    assert body["excluidas"] == EXCLUIDAS


def test_summary_chips_count_within_activas_and_name_the_excluded_sets(client, world):
    body = client.get("/api/v1/cases/summary", headers=headers(world["admin"].rut)).json()
    assert body["por_matriz"]["M3"] == 3
    assert body["terminada_sysgal"] == 1
    assert body["contrato_caducado"] == 1


@pytest.mark.parametrize(
    "params,keys",
    [
        ({}, {"activo", "moroso", "sin_dato"}),
        ({"contrato": "caducado"}, {"caducado"}),
        ({"contrato": "activo"}, {"activo"}),
        ({"contrato": "moroso"}, {"moroso"}),
        ({"contrato": "sin_dato"}, {"sin_dato"}),
        ({"terminada_sysgal": True}, {"terminada"}),
        ({"terminada_sysgal": False}, {"activo", "moroso", "sin_dato", "caducado"}),
        ({"matriz": "M3"}, {"activo", "moroso", "sin_dato"}),
        ({"q": "C-"}, {"activo", "moroso", "sin_dato", "caducado", "terminada"}),
    ],
)
def test_list_filters_keep_excluded_causas_reachable_only_through_their_own_filter(client, world, params, keys):
    body = client.get("/api/v1/cases", params=params, headers=headers(world["admin"].rut)).json()
    assert {i["id"] for i in body["items"]} == {world["cases"][k].id for k in keys}
    assert body["total"] == len(keys)


def test_list_items_carry_the_stored_state(client, world):
    items = {
        i["id"]: i
        for i in client.get("/api/v1/cases", params={"contrato": "caducado"}, headers=headers(world["admin"].rut)).json()["items"]
    }
    item = items[world["cases"]["caducado"].id]
    assert item["contrato_estado"] == "caducado" and item["es_activa"] is False


def test_bogus_contrato_is_422(client, world):
    assert client.get("/api/v1/cases?contrato=bogus", headers=headers(world["admin"].rut)).status_code == 422

