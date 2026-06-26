"""Client model — a person whose PJUD cases are tracked via their own Clave Única."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class Client(Base):
    """A client whose Clave Única credential is stored so the firm can scrape
    their PJUD cases automatically and attribute them to an assigned lawyer."""

    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)

    # Identity — not unique: the same person could appear under multiple entries
    rut = Column(String(12), index=True, nullable=True)
    nombre = Column(String(255), nullable=True)

    # Clave Única login — may differ from the Chilean national RUT
    clave_unica_rut = Column(String(12), nullable=True)
    encrypted_clave_unica_password = Column(String(512), nullable=True)

    # Firm relationship — which lawyer "owns" this client's cases
    assigned_lawyer_id = Column(
        Integer, ForeignKey("lawyers.id"), nullable=True, index=True
    )

    is_active = Column(Boolean, default=True, nullable=False)
    source = Column(String(50), default="crm", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    assigned_lawyer = relationship("Lawyer", foreign_keys=[assigned_lawyer_id])
    cases = relationship("Case", back_populates="client")
