"""CaseEscrito model - Filing records from the Escritos por Resolver tab."""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseEscrito(Base):
    """Filing (escrito) record scraped from the Escritos por Resolver tab of a PJUD case."""

    __tablename__ = "case_escritos"

    __table_args__ = (
        UniqueConstraint("case_id", "natural_key", name="uq_case_escrito_natural_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    # Entity fields from PJUD
    fecha_ingreso = Column(DateTime, nullable=True)                    # DD/MM/YYYY → parsed
    tipo_escrito = Column(String(255), nullable=False)
    solicitante = Column(String(500), nullable=False)
    tiene_documento = Column(Boolean, nullable=False, default=False)
    tiene_anexo = Column(Boolean, nullable=False, default=False)
    doc_token = Column(String(1024), nullable=True)                    # JWT stored, not fetched

    # Dedup spine
    natural_key = Column(String(64), nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    case = relationship("Case", back_populates="escritos")
