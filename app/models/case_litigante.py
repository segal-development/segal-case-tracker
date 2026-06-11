"""CaseLitigante model - Litigants/parties for a civil case."""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseLitigante(Base):
    """Litigant (party) record scraped from the Litigantes tab of a PJUD case."""

    __tablename__ = "case_litigantes"

    __table_args__ = (
        UniqueConstraint("case_id", "natural_key", name="uq_case_litigante_natural_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    # Entity fields from PJUD
    participante = Column(String(50), nullable=False)    # Role code: DTE., DDO., AB.DTE, …
    rut = Column(String(20), nullable=False)             # RUT with dv (may be "" when absent)
    persona_type = Column(String(20), nullable=False)    # JURIDICA or NATURAL
    nombre = Column(String(500), nullable=False)         # Full name or company name

    # Dedup spine — deterministic key computed in sync layer (see sync_service)
    natural_key = Column(String(64), nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    case = relationship("Case", back_populates="litigantes")
