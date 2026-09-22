"""Idempotent seeder for the matriz de clasificación reference tables.

Reads the three CSVs committed under ``app/data/matriz/`` (regenerated from
the business-owned xlsx via ``scripts/matriz_xlsx_to_csv.py`` for the first
two; hand-curated for the third) and upserts them into:

- ``matriz_clasificacion``   (natural key: proc_simple + etapa)
- ``matriz_tramite_override`` (natural key: proc_antiguo + etapa + nombre_tramite)
- ``matriz_pjud_mapeo``      (natural key: pjud_stage + match_tipo)

Safe to run repeatedly: rows are upserted by natural key, never duplicated.
Existing ``matriz_pjud_mapeo`` rows are updated in place except ``activo``,
which is preserved once set — a business-toggled deactivation must survive
re-seeding. Run via ``python scripts/seed_matriz.py`` or call
``seed_matriz(db)`` directly (e.g. from a migration data step or a test).
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.matriz_clasificacion import MatrizClasificacion
from app.models.matriz_pjud_mapeo import MATCH_TIPO_STAGE, MatrizPjudMapeo
from app.models.matriz_tramite_override import MatrizTramiteOverride

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "matriz"


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int_or_none(value: str | None) -> int | None:
    value = _blank_to_none(value)
    return int(value) if value is not None else None


def seed_clasificacion(db: Session) -> dict[str, int]:
    """Upsert ``matriz_clasificacion`` from ``clasificacion.csv``."""
    rows = _read_csv(DATA_DIR / "clasificacion.csv")

    existing = {
        (row.proc_simple, row.etapa): row
        for row in db.query(MatrizClasificacion).all()
    }

    created = 0
    updated = 0
    for r in rows:
        proc_simple = r["proc_simple"].strip()
        etapa = r["etapa"].strip()
        if not proc_simple or not etapa:
            continue  # skip malformed/blank rows defensively

        key = (proc_simple, etapa)
        values = dict(
            proc_antiguo=r["proc_antiguo"].strip(),
            etapa_padre=_blank_to_none(r["etapa_padre"]),
            orden=_int_or_none(r["orden"]),
            condicion_rol=_blank_to_none(r["condicion_rol"]),
            matriz=_blank_to_none(r["matriz"]),
            observaciones=_blank_to_none(r["observaciones"]),
        )

        row = existing.get(key)
        if row is None:
            row = MatrizClasificacion(proc_simple=proc_simple, etapa=etapa, **values)
            db.add(row)
            created += 1
        else:
            for field, value in values.items():
                setattr(row, field, value)
            updated += 1

    db.commit()
    logger.info("seed_matriz: clasificacion created=%s updated=%s", created, updated)
    return {"created": created, "updated": updated, "total": len(existing) + created}


def seed_tramite_overrides(db: Session) -> dict[str, int]:
    """Upsert ``matriz_tramite_override`` from the overridden rows of ``tramites.csv``."""
    rows = _read_csv(DATA_DIR / "tramites.csv")

    existing = {
        (row.proc_antiguo, row.etapa, row.nombre_tramite): row
        for row in db.query(MatrizTramiteOverride).all()
    }

    created = 0
    updated = 0
    for r in rows:
        matriz = _blank_to_none(r["matriz_especifica_opcional"])
        if matriz is None:
            continue  # only rows with an explicit override belong in this table

        proc_antiguo = r["procedimiento_antiguo"].strip()
        etapa = r["etapa"].strip()
        nombre_tramite = r["nombre_del_tramite"].strip()
        if not proc_antiguo or not etapa or not nombre_tramite:
            continue

        key = (proc_antiguo, etapa, nombre_tramite)
        values = dict(matriz=matriz, observaciones=_blank_to_none(r["observaciones"]))

        row = existing.get(key)
        if row is None:
            row = MatrizTramiteOverride(
                proc_antiguo=proc_antiguo, etapa=etapa, nombre_tramite=nombre_tramite, **values
            )
            db.add(row)
            created += 1
        else:
            for field, value in values.items():
                setattr(row, field, value)
            updated += 1

    db.commit()
    logger.info("seed_matriz: tramite_override created=%s updated=%s", created, updated)
    return {"created": created, "updated": updated, "total": len(existing) + created}


def seed_pjud_mapeo(db: Session) -> dict[str, int]:
    """Upsert ``matriz_pjud_mapeo`` from ``mapeo_pjud.csv``.

    ``activo`` is only set on CREATE — re-seeding never resurrects a stage the
    business has deliberately deactivated via the API. Natural key is
    (pjud_stage, match_tipo) — NOT pjud_stage alone, since the same text can
    legitimately be both a PJUD etapa and a fallback description substring
    (e.g. "Sentencia" is both).
    """
    rows = _read_csv(DATA_DIR / "mapeo_pjud.csv")

    existing = {
        (row.pjud_stage, row.match_tipo): row for row in db.query(MatrizPjudMapeo).all()
    }

    created = 0
    updated = 0
    for r in rows:
        pjud_stage = r["pjud_stage"].strip()
        matriz_etapa = r["matriz_etapa"].strip()
        if not pjud_stage or not matriz_etapa:
            continue

        match_tipo = _blank_to_none(r.get("match_tipo")) or MATCH_TIPO_STAGE
        orden = _int_or_none(r.get("orden")) or 0
        nota = _blank_to_none(r["nota"])

        key = (pjud_stage, match_tipo)
        row = existing.get(key)
        if row is None:
            row = MatrizPjudMapeo(
                pjud_stage=pjud_stage,
                matriz_etapa=matriz_etapa,
                nota=nota,
                activo=True,
                match_tipo=match_tipo,
                orden=orden,
            )
            db.add(row)
            created += 1
        else:
            row.matriz_etapa = matriz_etapa
            row.nota = nota
            row.orden = orden
            updated += 1

    db.commit()
    logger.info("seed_matriz: pjud_mapeo created=%s updated=%s", created, updated)
    return {"created": created, "updated": updated, "total": len(existing) + created}


def seed_matriz(db: Session) -> dict[str, dict[str, int]]:
    """Seed all three matriz reference tables. Idempotent — safe to re-run."""
    return {
        "clasificacion": seed_clasificacion(db),
        "tramite_override": seed_tramite_overrides(db),
        "pjud_mapeo": seed_pjud_mapeo(db),
    }
