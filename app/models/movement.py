"""Movement model - Case movements/actuaciones."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text

from sqlalchemy.orm import relationship

from app.core.database import Base


class Movement(Base):
    """Case movement/actuacion from PJUD."""
    
    __tablename__ = "movements"
    
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    
    # Movement details from PJUD
    stage = Column(String(255), nullable=True)  # Etapa
    procedure = Column(String(255), nullable=True)  # Tramite
    description = Column(Text, nullable=False)  # Descripcion
    folio = Column(String(50), nullable=True)  # Folio
    
    # Document link if available
    document_url = Column(String(1024), nullable=True)
    # Slice 1 bug fix: primary document JWT, denormalized for quick access.
    # Was silently dropped after parsing; now persisted in sync flow.
    document_token = Column(Text, nullable=True)
    
    # PJUD specific
    pjud_movement_id = Column(String(100), nullable=True)
    
    # Timestamps
    movement_date = Column(DateTime, nullable=False)  # Fecha del movimiento
    created_at = Column(DateTime, default=datetime.utcnow)  # When we detected it
    
    # Relationships
    case = relationship("Case", back_populates="movements")
