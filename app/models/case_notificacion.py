"""CaseNotificacion model - Notification records from the Notificaciones tab."""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseNotificacion(Base):
    """Notification record scraped from the Notificaciones tab of a PJUD case."""

    __tablename__ = "case_notificaciones"

    __table_args__ = (
        UniqueConstraint("case_id", "natural_key", name="uq_case_notificacion_natural_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    # Entity fields from PJUD
    rol = Column(String(50), nullable=False)
    estado_notif = Column(String(100), nullable=False)
    tipo_notif = Column(String(100), nullable=False)
    fecha_tramite = Column(DateTime, nullable=True)       # DD/MM/YYYY → parsed datetime
    tipo_participante = Column(String(50), nullable=False)
    nombre = Column(String(500), nullable=False)
    tramite = Column(String(255), nullable=False)
    obs_fallida = Column(Text, nullable=True)             # Observation for failed notifications

    # Dedup spine
    natural_key = Column(String(64), nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    case = relationship("Case", back_populates="notificaciones")
