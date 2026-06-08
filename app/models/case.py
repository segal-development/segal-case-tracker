"""Case model - Civil court cases."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Case(Base):
    """Civil court case tracked by a lawyer."""
    
    __tablename__ = "cases"
    
    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    court_id = Column(Integer, ForeignKey("courts.id"), nullable=False)
    
    # Case identification
    rol = Column(String(50), nullable=False, index=True)  # C-1234-2024
    rit = Column(String(50), nullable=True)  # Internal PJUD ID
    competencia = Column(String(20), default="civil")  # civil, laboral, penal
    
    # Parties
    plaintiff = Column(String(500), nullable=True)  # Demandante
    defendant = Column(String(500), nullable=True)  # Demandado
    
    # Case details
    matter = Column(String(255), nullable=True)  # Materia (Cobro de pesos, etc.)
    procedure = Column(String(255), nullable=True)  # Procedimiento
    status = Column(String(50), default="active")  # active, archived, closed
    
    # PJUD specific
    pjud_causa_id = Column(String(100), nullable=True)  # PJUD internal ID
    
    # Timestamps
    filed_at = Column(DateTime, nullable=True)  # Fecha de ingreso
    last_movement_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    lawyer = relationship("Lawyer", back_populates="cases")
    court = relationship("Court", back_populates="cases")
    movements = relationship("Movement", back_populates="case", order_by="desc(Movement.movement_date)")
    documents = relationship("Document", back_populates="case")
    alerts = relationship("Alert", back_populates="case")
