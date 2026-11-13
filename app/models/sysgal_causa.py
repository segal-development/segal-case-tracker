"""SysgalCausa — one row per causa reported by Sysgal ``GET /api_sync/causas``.

``case_id`` is the match to OUR ``Case`` (NULL while unmatched or ambiguous);
``match_method`` records which rule produced it.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, text

from app.core.database import Base


class SysgalCausa(Base):
    __tablename__ = "sysgal_causas"

    id = Column(Integer, primary_key=True, index=True)
    sysgal_id = Column(Integer, nullable=False, unique=True, index=True)

    rol = Column(String(255), nullable=False)  # Sysgal sends free-text ROLs too (60 chars seen)
    rol_norm = Column(String(255), nullable=False, index=True)
    tribunal = Column(String(255), nullable=False)
    tribunal_codigo = Column(String(20), nullable=True)
    rut_cliente = Column(String(30), nullable=True)

    abogado_sysgal_id = Column(Integer, nullable=True, index=True)
    abogado_nombre = Column(String(255), nullable=True)
    etapa = Column(String(255), nullable=True)
    matriz = Column(String(20), nullable=True)  # Sysgal's own matriz (M1 Baja/M1 Alta/M2/M3), when it sends one
    tramite = Column(String(255), nullable=True)
    estado = Column(String(50), nullable=True)
    competencia = Column(String(50), nullable=True)
    eliminada = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    responsable_desde = Column(Date, nullable=True)
    sysgal_updated_at = Column(DateTime, nullable=True)

    case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    match_method = Column(String(30), nullable=True)
    synced_at = Column(DateTime, nullable=False, default=datetime.utcnow)
