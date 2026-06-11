"""CaseExhorto model - Exhorto/rogatory letter records from the Exhortos tab."""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseExhorto(Base):
    """Exhorto record scraped from the Exhortos tab of a PJUD case."""

    __tablename__ = "case_exhortos"

    __table_args__ = (
        UniqueConstraint("case_id", "natural_key", name="uq_case_exhorto_natural_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    # Entity fields from PJUD
    rol_origen = Column(String(50), nullable=False)         # Originating case ROL
    tipo_exhorto = Column(String(100), nullable=False)      # e.g., ACTIVO
    rol_destino = Column(String(50), nullable=False)        # Destination ROL, e.g. E-355-2026
    fecha_ordena = Column(DateTime, nullable=True)          # Date ordered
    fecha_ingreso = Column(DateTime, nullable=True)         # Date received
    tribunal_destino = Column(String(500), nullable=False)  # Destination court name
    estado = Column(String(100), nullable=False)            # Current status

    # Dedup spine
    natural_key = Column(String(64), nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    case = relationship("Case", back_populates="exhortos")
