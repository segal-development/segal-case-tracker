"""Tests for the bonus engine (V1–V4 + liquidación).

Every tramo boundary and the full liquidación assembly are pinned to the exact
values in the firm's "SISTEMA DE HITOS" spreadsheet — this is payroll math.
"""
import pytest

from app.services import bono_calc as bc


# --- V1: $/cliente by activation tramo -------------------------------------- #
# Modelo V11 (§9.2): tabla V1 ÚNICA para todos los niveles; umbral 65%.
@pytest.mark.parametrize("nivel,pct,expected", [
    ("junior", 0.80, 8000), ("pleno", 0.80, 8000),
    ("junior", 0.95, 8000), ("pleno", 0.95, 8000),
    ("junior", 0.75, 6400), ("pleno", 0.75, 6400),  # V11: sin split por nivel
    ("junior", 0.79, 6400), ("pleno", 0.79, 6400),
    ("junior", 0.70, 4800), ("pleno", 0.70, 4800),
    ("junior", 0.65, 2400), ("pleno", 0.65, 2400),
    ("junior", 0.649, 0), ("pleno", 0.0, 0),
])
def test_valor_cliente_v1(nivel, pct, expected):
    assert bc.valor_cliente_v1(nivel, pct) == expected


def test_v1_matches_v11_liquidacion_examples():
    """Pin the exact tramos used in the V11 liquidación examples (§7.2)."""
    assert bc.valor_cliente_v1("junior", 0.670) == 2400   # Calderón, 65–69%
    assert bc.valor_cliente_v1("pleno", 0.716) == 4800    # Sandy, 70–74%
    assert bc.valor_cliente_v1("senior", 0.750) == 6400   # Pablo, 75–79%
    # V1 bruto = tramo × clientes activos (Sandy: 4800 × 53 = 254_400)
    sandy = bc.compute("pleno", clientes_m2=74, clientes_activos=53)
    assert sandy["v1_valor_cliente"] == 4800
    assert sandy["v1_bruto"] == 254_400


def test_v1_bruto_is_valor_times_activos():
    # 90 activos / 100 M-2 = 90% → ≥80% tramo → 8000 × 90
    b = bc.compute("junior", clientes_m2=100, clientes_activos=90)
    assert b["v1_pct_activacion"] == 0.90
    assert b["v1_valor_cliente"] == 8000
    assert b["v1_bruto"] == 8000 * 90


def test_v1_zero_when_no_m2():
    b = bc.compute("junior", clientes_m2=0, clientes_activos=5)
    assert b["v1_bruto"] == 0


# --- V3: tramo by compliance ------------------------------------------------ #
@pytest.mark.parametrize("nivel,pct,expected", [
    # Pleno con escala separada (×0.8077 del junior): 80769 / 64615 (V7.1).
    ("junior", 0.90, 100000), ("pleno", 0.90, 80769),
    ("junior", 0.80, 80000), ("pleno", 0.80, 64615),
    ("junior", 0.799, 0), ("pleno", 0.799, 0),
    ("junior", 1.0, 100000), ("pleno", 1.0, 80769),
])
def test_tramo_v3(nivel, pct, expected):
    assert bc.tramo_v3(nivel, pct) == expected


def test_v3_pleno_escala_separada_en_compute():
    # Pleno 100% cumpl → tramo 80769 (no 100000); 1 grave = -30% → 56538.
    b = bc.compute("pleno", causas_asignadas=10, causas_cumplidas=10)
    assert b["v3_tramo_bruto"] == 80769
    b2 = bc.compute("pleno", causas_asignadas=10, causas_cumplidas=10, reclamos_grave=1)
    assert b2["v3_neta"] == 56538  # 80769 × 0.70


# --- V4: penalty on V3 ------------------------------------------------------ #
def test_v4_penalty_reduces_v3():
    # junior, 100% cumpl → 100000; 1 grave = -30% → 70000
    b = bc.compute("junior", causas_asignadas=10, causas_cumplidas=10, reclamos_grave=1)
    assert b["v3_tramo_bruto"] == 100000
    assert b["v4_pct"] == pytest.approx(0.30)
    assert b["v3_neta"] == 70000


def test_v4_accumulates_and_floors_at_zero():
    # leve+medio+grave = 5+15+30 = 50%; plus 2 more graves = +60% → 110% → floor 0
    b = bc.compute(
        "junior", causas_asignadas=10, causas_cumplidas=10,
        reclamos_leve=1, reclamos_medio=1, reclamos_grave=3,
    )
    assert b["v4_pct"] == pytest.approx(0.05 + 0.15 + 3 * 0.30)
    assert b["v3_neta"] == 0  # MAX(0, 1 - 1.10)


# --- V2 --------------------------------------------------------------------- #
def test_v2_renovaciones():
    assert bc.compute("pleno", renovaciones=3)["v2_bruto"] == 3 * 10_400


# --- Full liquidación ------------------------------------------------------- #
def test_liquidacion_junior_full():
    b = bc.compute(
        "junior",
        clientes_m2=100, clientes_activos=90,       # V1: 8000×90 = 720_000
        causas_asignadas=100, causas_cumplidas=95,  # 95% → 100000
        renovaciones=2,                             # V2: 20_800
        hitos_aprobados=8_077,
    )
    assert b["fijo"] == 1_105_354
    assert b["v1_bruto"] == 720_000
    assert b["v3_neta"] == 100_000
    assert b["v2_bruto"] == 20_800
    assert b["total_bono_gestion"] == 720_000 + 100_000 + 20_800
    assert b["total_bruto"] == 1_105_354 + 8_077 + 720_000 + 100_000 + 20_800


def test_liquidacion_pleno_fijo():
    assert bc.compute("pleno")["fijo"] == 1_363_354


def test_unknown_nivel_defaults_junior():
    assert bc.compute("otro")["fijo"] == 1_105_354


# --- V3 desde avance semanal (suma de semanas) ------------------------------ #
def test_v3_desde_semanas_suma_a_tramo():
    # 4 semanas que suman 92% → tramo ≥90% (junior 100000)
    b = bc.compute("junior", cumpl_semanas=[30, 30, 20, 12])
    assert b["v3_pct_cumplimiento"] == pytest.approx(0.92)
    assert b["v3_tramo_bruto"] == 100000
    assert b["v3_neta"] == 100000


def test_v3_semanas_bajo_umbral_paga_cero():
    # suman 65% → <80% → tramo 0
    b = bc.compute("pleno", cumpl_semanas=[20, 20, 15, 10])
    assert b["v3_pct_cumplimiento"] == pytest.approx(0.65)
    assert b["v3_tramo_bruto"] == 0
    assert b["v3_neta"] == 0


def test_v3_semanas_con_penalizacion_v4():
    # 82% → tramo ≥80% junior 80000; 1 grave = -30% → 56000
    b = bc.compute("junior", cumpl_semanas=[40, 42], reclamos_grave=1)
    assert b["v3_tramo_bruto"] == 80000
    assert b["v3_neta"] == 56000


def test_v3_fallback_a_cociente_legacy_sin_semanas():
    # sin semanas → usa causas_cumplidas/asignadas (compat histórica)
    b = bc.compute("junior", causas_asignadas=100, causas_cumplidas=95)
    assert b["v3_pct_cumplimiento"] == pytest.approx(0.95)
    assert b["v3_neta"] == 100000


def test_v3_semanas_tienen_prioridad_sobre_legacy():
    # si hay semanas, ignoran el cociente legacy
    b = bc.compute("junior", cumpl_semanas=[50, 45], causas_asignadas=100, causas_cumplidas=10)
    assert b["v3_pct_cumplimiento"] == pytest.approx(0.95)  # 95% de semanas, no 10%
