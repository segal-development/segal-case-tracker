"""Court model - Chilean court reference data."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class Court(Base):
    """Chilean court reference data from PJUD."""
    
    __tablename__ = "courts"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    region = Column(String(50), nullable=False)  # RM, V, VIII, etc.
    type = Column(String(50), nullable=False)  # civil, familia, letras, etc.
    
    # PJUD internal identifiers
    pjud_competencia = Column(Integer, nullable=True)
    pjud_tribunal = Column(Integer, nullable=True)
    
    # Relationships
    cases = relationship("Case", back_populates="court")
