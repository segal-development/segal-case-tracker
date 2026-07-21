"""Contract renewals (renovaciones).

The firm's commercial side: when a client renews their service contract, we log
it here. Data entry is minimal — contract number, client RUT + name, and the
handling lawyer. Everything else is derived: a renewal always runs 12 months, so
``fecha_hasta = fecha_desde + 1 year``; the monthly ``monto_cuota`` (default
$25.000, editable) times 12 cuotas gives the annual ``total``.
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    DateTime,
    ForeignKey,
    text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base

CUOTAS_RENOVACION = 12  # every renewal is 12 months / 1 year


class Renovacion(Base):
    __tablename__ = "renovaciones"

    id = Column(Integer, primary_key=True, index=True)
    numero_contrato = Column(String(50), nullable=False, index=True)
    cliente_rut = Column(String(20), nullable=False, index=True)  # normalized
    cliente_nombre = Column(String(255), nullable=False)

    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False, index=True)  # abogado renovador

    fecha_desde = Column(Date, nullable=False, index=True)  # renewal date
    fecha_hasta = Column(Date, nullable=False)  # desde + 1 year

    monto_cuota = Column(Integer, nullable=False)  # CLP monthly
    cuotas = Column(Integer, nullable=False, server_default=text("12"), default=CUOTAS_RENOVACION)
    total = Column(Integer, nullable=False)  # monto_cuota * cuotas (snapshot)

    created_by_rut = Column(String(20), nullable=True)
    created_by_name = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lawyer = relationship("Lawyer")
