"""MatrizTramiteOverride — trámite-level matriz overrides.

Sourced from ``app/data/matriz/tramites.csv``, restricted to the rows where
"Matriz específica (opcional)" is set. Keyed by (proc_antiguo, etapa,
nombre_tramite): when a movement's trámite matches one of these rows, its
matriz OVERRIDES the etapa-level ``MatrizClasificacion.matriz`` value.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.core.database import Base


class MatrizTramiteOverride(Base):
    __tablename__ = "matriz_tramite_override"
    __table_args__ = (
        UniqueConstraint(
            "proc_antiguo", "etapa", "nombre_tramite",
            name="uq_matriz_tramite_override_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    proc_antiguo = Column(String(120), nullable=False)
    etapa = Column(String(120), nullable=False)
    nombre_tramite = Column(String(255), nullable=False)
    matriz = Column(String(20), nullable=False)
    observaciones = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
