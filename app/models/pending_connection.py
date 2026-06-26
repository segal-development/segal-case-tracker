"""PendingConnection model — ephemeral PJUD one-click connection queue row."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class PendingConnection(Base):
    """Tracks one-click PJUD connection requests from the browser through Carla.

    Security contract: no password or credential field is stored here.
    Only the captcha_token (ephemeral reCAPTCHA v3 token) is stored; it
    expires in ~120s and cannot be replayed as a credential (INV-1 / INV-4).
    """

    __tablename__ = "pending_connections"

    id = Column(Integer, primary_key=True, index=True)

    connection_id = Column(String(36), unique=True, index=True, nullable=False)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    rut = Column(String(12), nullable=False)
    auth_method = Column(String(50), nullable=False)
    captcha_token = Column(Text, nullable=True)  # ephemeral — NOT a credential

    status = Column(String(20), default="pending", index=True)
    session_id = Column(String(36), nullable=True)
    cases_synced = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    picked_at = Column(DateTime, nullable=True)

    lawyer = relationship("Lawyer", foreign_keys=[lawyer_id])
