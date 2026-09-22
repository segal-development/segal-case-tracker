"""MatrizPjudMapeo — PJUD movement -> matriz-taxonomy etapa.

Seeded once with the measured mapping (see ``app.services.matriz_seed``),
this is the layer the business iterates on: editable via
``PUT /api/v1/matriz/mapeo/{pjud_stage}`` without a deploy. ``activo=False``
rows are treated as unmapped by the classifier (same effect as no row at
all) instead of being deleted, to preserve history/notes.

Two match modes, selected by ``match_tipo`` (``pjud_stage`` holds the match
value in both cases — an exact stage string, or a description substring):

- ``"stage"`` (default): exact match against ``movements.stage``.
- ``"descripcion"``: accent-/case-insensitive SUBSTRING match against
  ``movements.description`` — the fallback layer for movements PJUD never
  tagged with a stage (measured on the live DB: ~5% of causas had a blank
  last-movement stage but a meaningful description, e.g. "Archivo del
  expediente en el Tribunal" -> CAUSA ARCHIVADA). Only consulted when the
  stage match fails (blank stage or no active stage row) — it never
  overrides a good stage match. ``orden`` gives these rules a deterministic
  evaluation order (ascending, ties broken by ``id``).
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint, text

from app.core.database import Base

MATCH_TIPO_STAGE = "stage"
MATCH_TIPO_DESCRIPCION = "descripcion"


class MatrizPjudMapeo(Base):
    __tablename__ = "matriz_pjud_mapeo"
    __table_args__ = (
        # (pjud_stage, match_tipo) is the natural key, NOT pjud_stage alone —
        # the same real-world text can legitimately be both a PJUD etapa and
        # a fallback description substring (e.g. "Sentencia" is both).
        UniqueConstraint(
            "pjud_stage", "match_tipo", name="uq_matriz_pjud_mapeo_stage_match_tipo"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    pjud_stage = Column(String(255), nullable=False, index=True)
    matriz_etapa = Column(String(120), nullable=False)
    nota = Column(Text, nullable=True)
    activo = Column(Boolean, nullable=False, server_default=text("true"), default=True)
    match_tipo = Column(String(20), nullable=False, server_default=text("'stage'"), default=MATCH_TIPO_STAGE)
    orden = Column(Integer, nullable=False, server_default=text("0"), default=0)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
