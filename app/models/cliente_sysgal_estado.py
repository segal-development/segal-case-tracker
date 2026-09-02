"""ClienteSysgalEstado model — per-RUT cache of the Sysgal CRM commercial state.

PRIVACY:
The Sysgal API returns the client's ``nombre``, ``email`` and ``telefono``.
Those are NEVER stored here (nor logged anywhere). This table only keeps the
commercial-state fields needed to derive the "cobertura" tag on causas.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String

from app.core.database import Base


class ClienteSysgalEstado(Base):
    """One row per demandado RUT looked up in Sysgal (upserted on every sync)."""

    __tablename__ = "cliente_sysgal_estados"

    id = Column(Integer, primary_key=True, index=True)

    # Canonical ``clean_rut`` form (``12345678-9``), the lookup key.
    rut = Column(String(20), nullable=False, unique=True, index=True)

    # False when Sysgal answered "Cliente no encontrado" — other fields stay NULL.
    encontrado = Column(Boolean, nullable=False)

    # ``estado_comercial_codigo`` (ACTIVO, MOROSO_INACTIVO, …) and its label.
    estado_codigo = Column(String(30), nullable=True, index=True)
    estado_label = Column(String(60), nullable=True)
    tiene_contrato = Column(Boolean, nullable=True)

    # ``contrato.vigencia_hasta`` — used to catch stale ACTIVO codes.
    vigencia_hasta = Column(Date, nullable=True)

    # Sysgal's own ``updated_at`` for the client record.
    sysgal_updated_at = Column(DateTime, nullable=True)

    # When WE last refreshed this row.
    synced_at = Column(DateTime, nullable=False, default=datetime.utcnow)
