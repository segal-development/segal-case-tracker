"""MatrizPjudMapeo — PJUD stage ("movements.stage") -> matriz-taxonomy etapa.

Seeded once with the measured mapping (see ``app.services.matriz_seed``),
this is the layer the business iterates on: editable via
``PUT /api/v1/matriz/mapeo/{pjud_stage}`` without a deploy. ``activo=False``
rows are treated as unmapped by the classifier (same effect as no row at
all) instead of being deleted, to preserve history/notes.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, text

from app.core.database import Base


class MatrizPjudMapeo(Base):
    __tablename__ = "matriz_pjud_mapeo"

    id = Column(Integer, primary_key=True, index=True)
    pjud_stage = Column(String(255), nullable=False, unique=True, index=True)
    matriz_etapa = Column(String(120), nullable=False)
    nota = Column(Text, nullable=True)
    activo = Column(Boolean, nullable=False, server_default=text("true"), default=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
