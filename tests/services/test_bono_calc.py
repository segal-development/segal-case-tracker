"""Tests for the bonus engine (V1–V4 + liquidación).

Every tramo boundary and the full liquidación assembly are pinned to the exact
values in the firm's "SISTEMA DE HITOS" spreadsheet — this is payroll math.
"""
import pytest

from app.services import bono_calc as bc


# --- V1: $/cliente by activation tramo -------------------------------------- #
@pytest.mark.parametrize("nivel,pct,expected", [
    ("junior", 0.80, 6462), ("pleno", 0.80, 6462),
    ("junior", 0.95, 6462), ("pleno", 0.95, 6462),
    ("junior", 0.75, 5162), ("pleno", 0.75, 5169),  # only tramo that differs by nivel
    ("junior", 0.79, 5162), ("pleno", 0.79, 5169),
    ("junior", 0.70, 3877), ("pleno", 0.70, 3877),
    ("junior", 0.65, 1938), ("pleno", 0.65, 1938),
    ("junior", 0.649, 0), ("pleno", 0.0, 0),
])
def test_valor_cliente_v1(nivel, pct, expected):
    assert bc.valor_cliente_v1(nivel, pct) == expected


def test_v1_bruto_is_valor_times_activos():
    # 90 activos / 100 M-2 = 90% → ≥80% tramo → 6462 × 90
    b = bc.compute("junior", clientes_m2=100, clientes_activos=90)
    assert b["v1_pct_activacion"] == 0.90
    assert b["v1_valor_cliente"] == 6462
    assert b["v1_bruto"] == 6462 * 90


def test_v1_zero_when_no_m2():
    b = bc.compute("junior", clientes_m2=0, clientes_activos=5)
    assert b["v1_bruto"] == 0


# --- V3: tramo by compliance ------------------------------------------------ #
@pytest.mark.parametrize("nivel,pct,expected", [
    ("junior", 0.90, 100000), ("pleno", 0.90, 80769),
    ("junior", 0.80, 80000), ("pleno", 0.80, 64615),
    ("junior", 0.799, 0), ("pleno", 0.799, 0),
    ("junior", 1.0, 100000), ("pleno", 1.0, 80769),
])
def test_tramo_v3(nivel, pct, expected):
    assert bc.tramo_v3(nivel, pct) == expected


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
        clientes_m2=100, clientes_activos=90,       # V1: 6462×90 = 581_580
        causas_asignadas=100, causas_cumplidas=95,  # 95% → 100000
        renovaciones=2,                             # V2: 20_800
        hitos_aprobados=8_077,
    )
    assert b["fijo"] == 1_105_354
    assert b["v1_bruto"] == 581_580
    assert b["v3_neta"] == 100_000
    assert b["v2_bruto"] == 20_800
    assert b["total_bono_gestion"] == 581_580 + 100_000 + 20_800
    assert b["total_bruto"] == 1_105_354 + 8_077 + 581_580 + 100_000 + 20_800


def test_liquidacion_pleno_fijo():
    assert bc.compute("pleno")["fijo"] == 1_363_354


def test_unknown_nivel_defaults_junior():
    assert bc.compute("otro")["fijo"] == 1_105_354
