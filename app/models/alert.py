"""Alert model - Movement alerts sent to lawyers."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Alert(Base):
    """Alert sent when new movement is detected."""
    
    __tablename__ = "alerts"
    
    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    movement_id = Column(Integer, ForeignKey("movements.id"), nullable=True)
    
    # Alert details
    type = Column(String(50), nullable=False)  # new_movement, status_change, etc.
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=True)
    
    # Delivery status
    email_sent = Column(Boolean, default=False)
    email_sent_at = Column(DateTime, nullable=True)
    webhook_sent = Column(Boolean, default=False)
    webhook_sent_at = Column(DateTime, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    lawyer = relationship("Lawyer", back_populates="alerts")
    case = relationship("Case", back_populates="alerts")
