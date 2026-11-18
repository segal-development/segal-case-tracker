"""SysgalContrato — one row per contract from Sysgal ``GET /api_sync/cuotas``.

Data minimization: only what the mora gate needs. No client name, RUT, cobrador/abogado or
amounts are stored. ``copia_at`` is the envelope's ``actualizado`` (Sysgal's daily ~07:00 copy; ``actualizacion`` is only text).
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String

from app.core.database import Base


class SysgalContrato(Base):
    __tablename__ = "sysgal_contratos"

    id = Column(Integer, primary_key=True, index=True)
    contrato = Column(String(50), nullable=False, unique=True, index=True)
    cuota_actual = Column(Integer, nullable=True)
    contrato_moroso = Column(Boolean, nullable=False, default=False)
    contrato_al_dia = Column(Boolean, nullable=False, default=False)
    id_causas = Column(JSON, nullable=False, default=list)  # Sysgal causa ids (SysgalCausa.sysgal_id)
    copia_at = Column(DateTime, nullable=True)
    synced_at = Column(DateTime, nullable=False, default=datetime.utcnow)
