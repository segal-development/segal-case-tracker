"""Bonus variables (V1–V4) — the manual monthly inputs per lawyer.

One row per (lawyer, período YYYY-MM). Dirección Jurídica enters the source
numbers (client counts, case counts, complaints, renewals); the system computes
V1–V4 and the liquidación from them (see ``app.services.bono_calc``). ``nivel``
is snapshotted at entry so a later tier change never retro-alters a closed
period. The bonus rule mirrors the firm's spreadsheet ("VARIABLES BONO").
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class BonoVariables(Base):
    __tablename__ = "bono_variables"
    __table_args__ = (
        UniqueConstraint("lawyer_id", "periodo", name="uq_bono_variables_lawyer_periodo"),
    )

    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False, index=True)
    periodo = Column(String(7), nullable=False, index=True)  # "YYYY-MM"
    nivel = Column(String(10), nullable=False)  # snapshot at entry

    # V1 — retención cliente M-2
    clientes_m2 = Column(Integer, nullable=False, server_default=text("0"), default=0)
    clientes_activos = Column(Integer, nullable=False, server_default=text("0"), default=0)

    # V3 — cumplimiento mensual.
    # Legacy: un solo cociente cumplidas/asignadas (se conserva para historial,
    # ya NO se edita). Nuevo: avance de cartera cargado por SEMANA (puntos de %,
    # ej. 7.5 = 7,5%); el % de cumplimiento del mes es la SUMA de las semanas.
    causas_asignadas = Column(Integer, nullable=False, server_default=text("0"), default=0)
    causas_cumplidas = Column(Integer, nullable=False, server_default=text("0"), default=0)
    cumpl_sem1 = Column(Float, nullable=False, server_default=text("0"), default=0.0)
    cumpl_sem2 = Column(Float, nullable=False, server_default=text("0"), default=0.0)
    cumpl_sem3 = Column(Float, nullable=False, server_default=text("0"), default=0.0)
    cumpl_sem4 = Column(Float, nullable=False, server_default=text("0"), default=0.0)
    cumpl_sem5 = Column(Float, nullable=False, server_default=text("0"), default=0.0)
    # Meta del mes (puntos de %) que carga Carla a mano; se compara con lo que
    # llevan (suma de semanas). Es referencia visual — NO altera el cálculo de V3.
    meta_cumplimiento = Column(Float, nullable=False, server_default=text("0"), default=0.0)

    # V4 — reclamos (counts by severity)
    reclamos_leve = Column(Integer, nullable=False, server_default=text("0"), default=0)
    reclamos_medio = Column(Integer, nullable=False, server_default=text("0"), default=0)
    reclamos_grave = Column(Integer, nullable=False, server_default=text("0"), default=0)

    # V2 — renovación contrato caducado (1ra cuota pagada)
    renovaciones = Column(Integer, nullable=False, server_default=text("0"), default=0)

    verificado_dj = Column(Boolean, nullable=False, server_default=text("false"), default=False)

    created_by_rut = Column(String(20), nullable=True)
    updated_by_rut = Column(String(20), nullable=True)
    updated_by_name = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lawyer = relationship("Lawyer")
