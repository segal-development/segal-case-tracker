"""SysgalIncorporacion — queue of Sysgal causas the firm holds that our system does not follow yet.

One row per ``SysgalCausa``. ``estado``: pendiente | incorporada | no_visible | sin_corte |
error | descartada. ``prioridad``: lower = first.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, text

from app.core.database import Base


class SysgalIncorporacion(Base):
    __tablename__ = "sysgal_incorporaciones"

    id = Column(Integer, primary_key=True, index=True)
    sysgal_causa_id = Column(
        Integer, ForeignKey("sysgal_causas.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    estado = Column(String(20), nullable=False, default="pendiente", server_default=text("'pendiente'"), index=True)
    prioridad = Column(Integer, nullable=False, default=0, server_default=text("0"))
    intentos = Column(Integer, nullable=False, default=0, server_default=text("0"))
    ultimo_error = Column(String(255), nullable=True)
    proximo_intento_at = Column(DateTime, nullable=True)
    # UTC time of the last PJUD attempt; the pace quota is counted from it (no in-memory counters).
    ultimo_intento_at = Column(DateTime, nullable=True, index=True)
    corte = Column(Integer, nullable=True)
    court_id = Column(Integer, ForeignKey("courts.id"), nullable=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
