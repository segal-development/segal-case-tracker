"""Webhook model - Custom webhook configurations."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, JSON
from sqlalchemy.orm import relationship

from app.core.database import Base


class Webhook(Base):
    """Webhook configuration for receiving events."""
    
    __tablename__ = "webhooks"
    
    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    
    # Webhook config
    url = Column(String(1024), nullable=False)
    events = Column(JSON, nullable=False)  # ["movement.new", "case.status_change"]
    secret = Column(String(255), nullable=True)  # HMAC signing secret
    
    # Status
    is_active = Column(Boolean, default=True)
    last_triggered_at = Column(DateTime, nullable=True)
    failure_count = Column(Integer, default=0)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    lawyer = relationship("Lawyer", back_populates="webhooks")
