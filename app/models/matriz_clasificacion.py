"""MatrizClasificacion — the business-owned procedimiento/etapa -> matriz taxonomy.

One row per (proc_simple, etapa) pair, sourced from
``app/data/matriz/clasificacion.csv`` (see ``app.services.matriz_seed``).
``matriz`` is nullable: some etapas legitimately have none assigned (e.g.
CAUSA ARCHIVADA), meaning "no active matriz workload" rather than "unknown".
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.core.database import Base


class MatrizClasificacion(Base):
    __tablename__ = "matriz_clasificacion"
    __table_args__ = (
        UniqueConstraint("proc_simple", "etapa", name="uq_matriz_clasificacion_proc_simple_etapa"),
    )

    id = Column(Integer, primary_key=True, index=True)
    proc_simple = Column(String(120), nullable=False, index=True)  # e.g. "Juicio Ejecutivo Completo"
    proc_antiguo = Column(String(120), nullable=False)  # e.g. "JUICIO EJECUTIVO"
    etapa = Column(String(120), nullable=False)  # matriz-taxonomy etapa, e.g. "DEMANDA NOTIFICADA"
    etapa_padre = Column(String(120), nullable=True)
    orden = Column(Integer, nullable=True)
    condicion_rol = Column(String(40), nullable=True)  # Cualquiera | Con ROL | Sin ROL
    matriz = Column(String(20), nullable=True)  # M1 Baja | M1 Alta | M2 | M3 | None
    observaciones = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
