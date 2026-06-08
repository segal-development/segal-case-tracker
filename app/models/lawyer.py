"""Lawyer model - Authenticated users."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import relationship

from app.core.database import Base


class Lawyer(Base):
    """Lawyer/user account linked to PJUD credentials."""
    
    __tablename__ = "lawyers"
    
    id = Column(Integer, primary_key=True, index=True)
    rut = Column(String(12), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)
    
    # Encrypted PJUD password for session refresh
    encrypted_pjud_password = Column(String(512), nullable=True)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    
    # Relationships
    cases = relationship("Case", back_populates="lawyer")
    webhooks = relationship("Webhook", back_populates="lawyer")
    alerts = relationship("Alert", back_populates="lawyer")
