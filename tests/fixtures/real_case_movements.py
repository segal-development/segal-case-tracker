"""Committed test fixture: real movement sequences from QA-scraped cases.

Source: scripts/manual/real_movements.json (gitignored — real PJUD data).
ROLs are anonymized (C-0001-2026, etc.) but stage/proc/desc text is kept
verbatim so classifier rules can be validated against actual PJUD output.

Each RealCaseFixture records:
  case_id          — anonymized rol
  movements        — list of FakeMovement (same attrs as Movement model)
  expected_state   — ProceduralState that the classifier should return
  expected_deadline — DeadlineType that should appear in the triggers dict,
                      or None when no active deadline is expected from the
                      current state's triggering rule

CRITICAL: any change that causes a case here to flip to an unexpected state
is a classifier regression and must be investigated before merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Optional

from app.core.deadlines_config import DeadlineType, ProceduralState


# ---------------------------------------------------------------------------
# Minimal stub — matches the attributes consumed by MovementClassifier
# ---------------------------------------------------------------------------


@dataclass
class FakeMovement:
    """Lightweight Movement substitute for unit tests (no DB dependency)."""

    stage: str
    procedure: str
    description: str
    movement_date: datetime
    id: int = 0
    case_id: int = 0


# ---------------------------------------------------------------------------
# Fixture type
# ---------------------------------------------------------------------------


class RealCaseFixture(NamedTuple):
    """One real case's movement sequence and expected classifier output."""

    case_id: str
    movements: list[FakeMovement]
    expected_state: ProceduralState
    # DeadlineType that the classifier must insert into the triggers dict
    # when the current state starts a deadline, else None.
    expected_deadline: Optional[DeadlineType]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(s: str) -> datetime:
    """Parse 'YYYY-MM-DD' into a datetime at midnight."""
    return datetime.strptime(s, "%Y-%m-%d")


def _mvs(*rows: tuple[str, str, str, str]) -> list[FakeMovement]:
    """Build FakeMovement list from (date, stage, proc, desc) tuples."""
    return [
        FakeMovement(
            stage=stage,
            procedure=proc,
            description=desc,
            movement_date=_dt(date),
        )
        for date, stage, proc, desc in rows
    ]


# ---------------------------------------------------------------------------
# Real case fixtures (anonymized rols, verbatim stage/proc/desc)
# ---------------------------------------------------------------------------

# C-0001-2026 (source: C-394-2026)
# Last state: Excepciones stage → EXCEPCIONES.
# NOTE: TRASLADO_EJECUTANTE_4D is no longer triggered at EXCEPCIONES stage —
# the trigger was moved to the Contestación Excepciones movement (Rule 4) so
# the 4-day countdown starts from the court's traslado resolution, not the
# excepciones filing.  No active deadline while waiting for court to act.
CASE_0001 = RealCaseFixture(
    case_id="C-0001-2026",
    movements=_mvs(
        ("2026-03-05", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-04-09", "Exhorto", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:08/04/2026 11:47"),
        ("2026-04-13", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-05-06", "Excepciones", "Escrito", "Devuelve Exhorto"),
        ("2026-05-06", "Excepciones", "Resolución", "Recepciona exhorto"),
        ("2026-05-07", "Excepciones", "Resolución", "Falla recurso de reposición"),
        ("2026-05-07", "", "", "Opone excepciones-Traslado"),
    ),
    expected_state=ProceduralState.EXCEPCIONES,
    expected_deadline=None,
)

# C-0002-2026 (source: C-2946-2026)
# Last state: Notificación resolución que recibe la causa a prue → AUTO_PRUEBA
CASE_0002 = RealCaseFixture(
    case_id="C-0002-2026",
    movements=_mvs(
        ("2026-03-12", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-03-23", "Exhorto", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Certificación) Diligencia:20/03/2026 12:03"),
        ("2026-03-27", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-09", "Excepciones", "Resolución", "Opone excepciones-Traslado"),
        ("2026-05-05", "Contestación Excepciones", "Escrito", "Tenga presente"),
        ("2026-05-14", "Contestación Excepciones", "Resolución", "Se pronuncia sobre admisibilidad excep."),
        ("2026-05-14", "", "", "Recibe la causa a prueba"),
        ("2026-05-30", "Contestación Excepciones", "Actuación Receptor", "Notificación resolución que recibe la causa a prue (Exitosa) Diligencia:27/05/20"),
    ),
    expected_state=ProceduralState.AUTO_PRUEBA,
    expected_deadline=DeadlineType.TERMINO_PROBATORIO_10D,
)

# C-0003-2025 (source: C-17041-2025)
# Oposición al remate stage — no matching rule → INDETERMINATE
CASE_0003 = RealCaseFixture(
    case_id="C-0003-2025",
    movements=_mvs(
        ("2026-01-16", "Requerimiento pago", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Certificación) Diligencia:15/01/2026 12:19"),
        ("2026-01-29", "Requerimiento pago", "Actuación Receptor", "Requerimiento de Pago (Ficto) Diligencia:26/01/2026 18:18"),
        ("2026-03-05", "Requerimiento pago", "Resolución", "Requerimiento de pago"),
        ("2026-03-06", "Requerimiento pago", "Resolución", "Decreta el remate"),
        ("2026-03-06", "Oposición al remate", "", "Certificado."),
        ("2026-03-16", "Oposición al remate", "Escrito", "Opone excepciones"),
        ("2026-06-09", "Oposición al remate", "Resolución", "Confiere traslado oposición al remate"),
    ),
    expected_state=ProceduralState.INDETERMINATE,
    expected_deadline=None,
)

# C-0004-2025 (source: C-7833-2025)
# NOTIFICADO → Excepciones → Contestación Excepciones → TRASLADO_EJECUTANTE
# TRASLADO_EJECUTANTE_4D is now triggered when entering TRASLADO_EJECUTANTE
# (from the first Contestación Excepciones movement), NOT at EXCEPCIONES stage.
CASE_0004 = RealCaseFixture(
    case_id="C-0004-2025",
    movements=_mvs(
        ("2026-02-02", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-03-18", "Exhorto", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:16/03/2026 11:23"),
        ("2026-03-20", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-05-06", "Excepciones", "Resolución", "Pide cuenta exhorto"),
        ("2026-05-06", "", "", "Opone excepciones-Traslado"),
        ("2026-05-08", "Contestación Excepciones", "Escrito", "Devuelve Exhorto"),
        ("2026-05-11", "Contestación Excepciones", "Resolución", "Recepciona exhorto"),
    ),
    expected_state=ProceduralState.TRASLADO_EJECUTANTE,
    expected_deadline=DeadlineType.TRASLADO_EJECUTANTE_4D,  # started when EXCEPCIONES was entered
)

# C-0005-2026 (source: C-2792-2026)
# Contestación Excepciones last stage → TRASLADO_EJECUTANTE
CASE_0005 = RealCaseFixture(
    case_id="C-0005-2026",
    movements=_mvs(
        ("2026-03-05", "", "", "Ordena despachar mandamiento"),
        ("2026-03-27", "Notificación demanda y su proveído", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:19/03/2026 11:49"),
        ("2026-04-02", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-21", "Excepciones", "Resolución", "Opone excepciones-Traslado"),
        ("2026-04-24", "Contestación Excepciones", "Escrito", "Evacúa traslado"),
        ("2026-04-30", "Contestación Excepciones", "Resolución", "Recibe la causa a prueba"),
    ),
    expected_state=ProceduralState.TRASLADO_EJECUTANTE,
    expected_deadline=DeadlineType.TRASLADO_EJECUTANTE_4D,
)

# C-0006-2026 (source: C-3462-2026)
# Still in Excepciones stage → EXCEPCIONES (no active deadline — see CASE_0001 note)
CASE_0006 = RealCaseFixture(
    case_id="C-0006-2026",
    movements=_mvs(
        ("2026-03-24", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-04-15", "Exhorto", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:14/04/2026 14:34"),
        ("2026-04-24", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-05-04", "Excepciones", "Resolución", "Mero trámite"),
        ("2026-06-05", "Excepciones", "Escrito", "Devuelve Exhorto"),
        ("2026-06-09", "Excepciones", "Resolución", "Recepciona exhorto"),
    ),
    expected_state=ProceduralState.EXCEPCIONES,
    expected_deadline=None,
)

# C-0007-2025 (source: C-17143-2025)
# Notificación resolución que recibe la causa a prue → AUTO_PRUEBA
CASE_0007 = RealCaseFixture(
    case_id="C-0007-2025",
    movements=_mvs(
        ("2026-01-06", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-03-12", "Notificación demanda y su proveído", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:11/03/2026 11:25"),
        ("2026-03-20", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-16", "Excepciones", "Resolución", "Opone excepciones-Traslado"),
        ("2026-04-20", "Contestación Excepciones", "Escrito", "Evacúa traslado"),
        ("2026-05-04", "Contestación Excepciones", "Resolución", "Recibe la causa a prueba"),
        ("2026-06-03", "Contestación Excepciones", "Actuación Receptor", "Notificación resolución que recibe la causa a prue (Exitosa) Diligencia:02/06/20"),
    ),
    expected_state=ProceduralState.AUTO_PRUEBA,
    expected_deadline=DeadlineType.TERMINO_PROBATORIO_10D,
)

# C-0008-2025 (source: C-4672-2025)
# Excepciones stage with only Mero trámite movements → EXCEPCIONES (no active deadline)
CASE_0008 = RealCaseFixture(
    case_id="C-0008-2025",
    movements=_mvs(
        ("2026-01-14", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-03-10", "Notificación demanda y su proveído", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:09/03/2026 16:33"),
        ("2026-03-17", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-03-18", "Excepciones", "Resolución", "Previo a proveer"),
        ("2026-03-24", "Excepciones", "Resolución", "Mero trámite"),
    ),
    expected_state=ProceduralState.EXCEPCIONES,
    expected_deadline=None,
)

# C-0009-2026 (source: C-3982-2026)
# Excepciones stage → EXCEPCIONES (no active deadline — see CASE_0001 note)
CASE_0009 = RealCaseFixture(
    case_id="C-0009-2026",
    movements=_mvs(
        ("2026-03-24", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-04-02", "Exhorto", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:31/03/2026 13:25"),
        ("2026-04-13", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-14", "Excepciones", "Resolución", "Apercibimiento poder y/o título"),
        ("2026-04-17", "Excepciones", "Escrito", "Devuelve Exhorto"),
        ("2026-04-20", "Excepciones", "Resolución", "Recepciona exhorto"),
    ),
    expected_state=ProceduralState.EXCEPCIONES,
    expected_deadline=None,
)

# C-0010-2025 (source: C-17026-2025)
# Full path to AUTO_PRUEBA via Notificación resolución que recibe
CASE_0010 = RealCaseFixture(
    case_id="C-0010-2025",
    movements=_mvs(
        ("2025-12-29", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-02-02", "Notificación demanda y su proveído", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:30/01/2026 19:32"),
        ("2026-02-04", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-02-10", "Excepciones", "Resolución", "Opone excepciones-Traslado"),
        ("2026-02-11", "Contestación Excepciones", "Escrito", "Evacúa traslado"),
        ("2026-02-16", "Contestación Excepciones", "Resolución", "Recibe la causa a prueba"),
        ("2026-02-23", "Contestación Excepciones", "Actuación Receptor", "Notificación resolución que recibe la causa a prue (Exitosa) Diligencia:20/02/20"),
        ("2026-04-16", "Contestación Excepciones", "Resolución", "Notifica expresamente"),
    ),
    expected_state=ProceduralState.AUTO_PRUEBA,
    expected_deadline=DeadlineType.TERMINO_PROBATORIO_10D,
)

# C-0011-2026 (source: C-3997-2026)
# Medida prejudicial: "Cita a Audiencia" appears before NOTIFICACIÓN DE DEMANDA.
# After fix #6 (Rule 7 min_state=NOTIFICADO), "Cita a Audiencia" no longer fires
# CITACION_SENTENCIA when the case is still INDETERMINATE, preventing the false
# SENTENCIA_10D ROJO.  The NOTIFICACIÓN DE DEMANDA (Exitosa) then fires NOTIFICADO.
# NOTE: The NOTIFICACIÓN here is likely notification of the medida prejudicial
# decision, not the initial demand — full medida-prejudicial classification is
# deferred to a future PR.  The important fix is that no false SENTENCIA_10D fires.
CASE_0011 = RealCaseFixture(
    case_id="C-0011-2026",
    movements=_mvs(
        ("2026-04-01", "Presentación de la Medida Prejudicial", "Resolución", "Cita a Audiencia"),
        ("2026-04-30", "Tramitación", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:29/04/2026 16:34"),
        ("2026-05-18", "Tramitación", "Resolución", "Mero trámite"),
        ("2026-06-05", "Tramitación", "Resolución", "Mero trámite"),
    ),
    expected_state=ProceduralState.NOTIFICADO,
    expected_deadline=DeadlineType.EXCEPCIONES_8D,
)

# C-0012-2026 (source: C-3887-2026)
# MANDAMIENTO (desc) → EXCEPCIONES stage (no active deadline — see CASE_0001 note)
CASE_0012 = RealCaseFixture(
    case_id="C-0012-2026",
    movements=_mvs(
        ("2026-03-26", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-04-13", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-14", "Excepciones", "", "Acredita Poder"),
        ("2026-04-15", "Excepciones", "Resolución", "Mero trámite"),
        ("2026-04-18", "Excepciones", "Escrito", "Devuelve Exhorto"),
        ("2026-04-21", "Excepciones", "Resolución", "Recepciona exhorto"),
    ),
    expected_state=ProceduralState.EXCEPCIONES,
    expected_deadline=None,
)

# C-0013-2026 (source: C-549-2026)
# NOTIFICACIÓN DE DEMANDA (Exitosa) → exceptions filed but stage remains
# "Notificación demanda y su proveído" (not "Excepciones") → NOTIFICADO
CASE_0013 = RealCaseFixture(
    case_id="C-0013-2026",
    movements=_mvs(
        ("2026-03-09", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-04-01", "Notificación demanda y su proveído", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:30/03/2026 08:10"),
        ("2026-04-10", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-14", "Notificación demanda y su proveído", "Resolución", "Previo a proveer"),
        ("2026-05-08", "Notificación demanda y su proveído", "Resolución", "No ha lugar"),
    ),
    expected_state=ProceduralState.NOTIFICADO,
    expected_deadline=DeadlineType.EXCEPCIONES_8D,
)

# C-0014-2026 (source: C-3650-2026)
# NOTIFICACIÓN DE DEMANDA (Exitosa) — no Excepciones stage follows → NOTIFICADO
CASE_0014 = RealCaseFixture(
    case_id="C-0014-2026",
    movements=_mvs(
        ("2026-04-08", "Presentación de la Medida Prejudicial", "Resolución", "Da curso a la medida prejudicial"),
        ("2026-05-14", "Tramitación", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:12/05/2026 11:44"),
        ("2026-05-19", "Tramitación", "Resolución", "Mero trámite"),
        ("2026-06-15", "Tramitación", "Resolución", "Mero trámite"),
    ),
    expected_state=ProceduralState.NOTIFICADO,
    expected_deadline=DeadlineType.EXCEPCIONES_8D,
)

# C-0015-2026 (source: C-675-2026)
# Excepciones stage → EXCEPCIONES (no active deadline — see CASE_0001 note)
CASE_0015 = RealCaseFixture(
    case_id="C-0015-2026",
    movements=_mvs(
        ("2026-03-24", "Inicio de la Tramitación", "Resolución", "Ordena despachar mandamiento"),
        ("2026-04-01", "Notificación demanda y su proveído", "Actuación Receptor", "NOTIFICACIÓN DE DEMANDA (Exitosa) Diligencia:30/03/2026 08:10"),
        ("2026-04-10", "Notificación demanda y su proveído", "Escrito", "Opone excepciones"),
        ("2026-04-14", "Excepciones", "Resolución", "Previo a proveer"),
        ("2026-04-16", "Excepciones", "", "Acredita Poder"),
        ("2026-04-21", "Excepciones", "Resolución", "Opone excepciones-Traslado"),
    ),
    expected_state=ProceduralState.EXCEPCIONES,
    expected_deadline=None,
)

# ---------------------------------------------------------------------------
# Collected fixture list for parametrized tests
# ---------------------------------------------------------------------------

ALL_REAL_CASES: list[RealCaseFixture] = [
    CASE_0001,
    CASE_0002,
    CASE_0003,
    CASE_0004,
    CASE_0005,
    CASE_0006,
    CASE_0007,
    CASE_0008,
    CASE_0009,
    CASE_0010,
    CASE_0011,
    CASE_0012,
    CASE_0013,
    CASE_0014,
    CASE_0015,
]

# Subset with deterministic expected state (no INDETERMINATE)
DETERMINISTIC_CASES: list[RealCaseFixture] = [
    c for c in ALL_REAL_CASES if c.expected_state != ProceduralState.INDETERMINATE
]
