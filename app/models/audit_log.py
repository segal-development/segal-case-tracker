"""Audit log model - Track all actions."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Text

from app.core.database import Base


class AuditLog(Base):
    """Audit log for tracking all system actions."""
    
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True)
    
    # Action details
    action = Column(String(100), nullable=False)  # login, case.create, scrape.start, etc.
    resource_type = Column(String(50), nullable=True)  # case, movement, webhook, etc.
    resource_id = Column(Integer, nullable=True)
    
    # Request info
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(512), nullable=True)
    
    # Details
    details = Column(JSON, nullable=True)  # Additional context
    error = Column(Text, nullable=True)  # Error message if failed
    
    # Timestamp
    created_at = Column(DateTime, default=datetime.utcnow)
