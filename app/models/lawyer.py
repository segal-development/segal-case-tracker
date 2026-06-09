"""Lawyer model - Authenticated users."""

from datetime import datetime
from enum import Enum

from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import relationship

from app.core.database import Base


class AuthMethod(str, Enum):
    """Authentication method for PJUD login."""
    CAPTCHA = "captcha"
    CLAVE_UNICA = "clave_unica"


class Lawyer(Base):
    """Lawyer/user account linked to PJUD credentials."""
    
    __tablename__ = "lawyers"
    
    id = Column(Integer, primary_key=True, index=True)
    rut = Column(String(12), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)
    
    # Encrypted PJUD password for session refresh (captcha method)
    encrypted_pjud_password = Column(String(512), nullable=True)
    
    # Clave Única authentication fields
    clave_unica_rut = Column(String(12), nullable=True)  # May differ from PJUD RUT
    encrypted_clave_unica_password = Column(String(512), nullable=True)
    preferred_auth_method = Column(String(20), default=AuthMethod.CAPTCHA.value)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    
    # Relationships
    cases = relationship("Case", back_populates="lawyer")
    webhooks = relationship("Webhook", back_populates="lawyer")
    alerts = relationship("Alert", back_populates="lawyer")
