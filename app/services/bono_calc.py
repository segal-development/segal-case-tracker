"""Bonus variable engine (V1–V4) + monthly liquidación.

Pure, side-effect-free replication of the firm's "SISTEMA DE HITOS" spreadsheet
(sheets VARIABLES BONO + LIQUIDACIÓN MENSUAL). The manual inputs (client counts,
case counts, complaints, renewals) are captured by Dirección Jurídica; this
module does every formula so the payroll math is deterministic and auditable.

Monthly total per lawyer:
    Total bruto = Fijo(nivel) + Hitos H1 aprobados + V1 + V3_neta + V2

Formulas (verbatim from the sheet):
  V1  Retención  = clientes_activos × $/cliente(tramo de %activación)
                   %activación = activos / M-2 total
                   $/cliente:  ≥80%→6462 · ≥75%→5162(J)/5169(P) · ≥70%→3877
                               ≥65%→1938 · <65%→0
  V3  Cumplimiento = V3_tramo × MAX(0, 1 − %V4)
                   %cumpl = causas con mov. útil / causas asignadas
                   V3_tramo:   ≥90%→100000(J)/80769(P) · ≥80%→80000(J)/64615(P) · <80%→0
  V4  Reclamos   %V4 = leves×5% + medios×15% + graves×30%
  V2  Renovación = renovaciones × $10.400
"""
from __future__ import annotations

from typing import TypedDict

JUNIOR = "junior"
PLENO = "pleno"

FIJO = {JUNIOR: 1_105_354, PLENO: 1_363_354}

V2_POR_RENOVACION = 10_400

# V4 penalty weights on V3 (per complaint, accumulable).
V4_PESO_LEVE = 0.05
V4_PESO_MEDIO = 0.15
V4_PESO_GRAVE = 0.30


def _round_clp(x: float) -> int:
    """CLP is integer; round half-up to the nearest peso."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def valor_cliente_v1(nivel: str, pct_activacion: float) -> int:
    """$/cliente by activation-rate tramo (only the 75–79% tramo differs by nivel)."""
    if pct_activacion >= 0.80:
        return 6462
    if pct_activacion >= 0.75:
        return 5169 if nivel == PLENO else 5162
    if pct_activacion >= 0.70:
        return 3877
    if pct_activacion >= 0.65:
        return 1938
    return 0


def tramo_v3(nivel: str, pct_cumplimiento: float) -> int:
    """V3 gross tramo by compliance-rate."""
    if pct_cumplimiento >= 0.90:
        return 80769 if nivel == PLENO else 100000
    if pct_cumplimiento >= 0.80:
        return 64615 if nivel == PLENO else 80000
    return 0


class BonoBreakdown(TypedDict):
    nivel: str
    fijo: int
    # V1
    v1_pct_activacion: float
    v1_valor_cliente: int
    v1_bruto: int
    # V3 / V4
    v3_pct_cumplimiento: float
    v3_tramo_bruto: int
    v4_pct: float
    v3_neta: int
    # V2
    v2_bruto: int
    # Totals
    hitos_aprobados: int
    total_bono_gestion: int  # V1 + V3_neta + V2 (as in the sheet's "TOTAL BONO GESTIÓN")
    total_bruto: int  # Fijo + Hitos + bono


def compute(
    nivel: str,
    *,
    clientes_m2: int = 0,
    clientes_activos: int = 0,
    causas_asignadas: int = 0,
    causas_cumplidas: int = 0,
    reclamos_leve: int = 0,
    reclamos_medio: int = 0,
    reclamos_grave: int = 0,
    renovaciones: int = 0,
    hitos_aprobados: int = 0,
) -> BonoBreakdown:
    """Full V1–V4 + liquidación breakdown for one lawyer/period."""
    nivel = PLENO if nivel == PLENO else JUNIOR  # anything not "pleno" is junior

    # V1 — retención
    pct_act = (clientes_activos / clientes_m2) if clientes_m2 > 0 else 0.0
    valor_cli = valor_cliente_v1(nivel, pct_act)
    v1 = clientes_activos * valor_cli

    # V3 — cumplimiento, minus V4 penalties
    pct_cumpl = (causas_cumplidas / causas_asignadas) if causas_asignadas > 0 else 0.0
    v3_tramo = tramo_v3(nivel, pct_cumpl)
    pct_v4 = (
        reclamos_leve * V4_PESO_LEVE
        + reclamos_medio * V4_PESO_MEDIO
        + reclamos_grave * V4_PESO_GRAVE
    )
    v3_neta = _round_clp(v3_tramo * max(0.0, 1.0 - pct_v4))

    # V2 — renovación
    v2 = renovaciones * V2_POR_RENOVACION

    fijo = FIJO[nivel]
    total_bono = v1 + v3_neta + v2
    total_bruto = fijo + hitos_aprobados + total_bono

    return BonoBreakdown(
        nivel=nivel,
        fijo=fijo,
        v1_pct_activacion=pct_act,
        v1_valor_cliente=valor_cli,
        v1_bruto=v1,
        v3_pct_cumplimiento=pct_cumpl,
        v3_tramo_bruto=v3_tramo,
        v4_pct=pct_v4,
        v3_neta=v3_neta,
        v2_bruto=v2,
        hitos_aprobados=hitos_aprobados,
        total_bono_gestion=total_bono,
        total_bruto=total_bruto,
    )
