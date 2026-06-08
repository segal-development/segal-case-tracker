"""Document model - Downloaded case documents."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, BigInteger
from sqlalchemy.orm import relationship

from app.core.database import Base


class Document(Base):
    """Document downloaded from PJUD and stored in GCS."""
    
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    movement_id = Column(Integer, ForeignKey("movements.id"), nullable=True)
    
    # Document info
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    
    # Storage
    gcs_path = Column(String(512), nullable=True)  # GCS bucket path
    pjud_url = Column(String(1024), nullable=True)  # Original PJUD URL
    
    # Timestamps
    document_date = Column(DateTime, nullable=True)  # Date on document
    downloaded_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    case = relationship("Case", back_populates="documents")
