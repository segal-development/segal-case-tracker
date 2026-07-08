"""Alert model - Movement alerts sent to lawyers."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, Text, text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Alert(Base):
    """Alert sent when new movement is detected."""
    
    __tablename__ = "alerts"
    
    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    movement_id = Column(Integer, ForeignKey("movements.id"), nullable=True)

    # Polymorphic entity target (ADR-003)
    # Identifies the non-movement entity that triggered this alert.
    # movement_id covers the Movement path; these two columns cover all others.
    entity_type = Column(String(30), nullable=True)   # "notificacion" | "escrito" | "exhorto"
    entity_id = Column(Integer, nullable=True)         # PK in the corresponding entity table

    # Alert details
    type = Column(String(50), nullable=False)  # new_movement, status_change, etc.
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=True)
    
    # Delivery status
    email_sent = Column(Boolean, default=False)
    email_sent_at = Column(DateTime, nullable=True)
    webhook_sent = Column(Boolean, default=False)
    webhook_sent_at = Column(DateTime, nullable=True)

    # In-app alert feed read/unread state (migration 026). Existing rows
    # backfill to read=False via the column's server_default.
    read = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    read_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    lawyer = relationship("Lawyer", back_populates="alerts")
    case = relationship("Case", back_populates="alerts")
