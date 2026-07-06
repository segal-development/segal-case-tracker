"""CaseLawyerSource model - per-lawyer sync-source association (ADR-002).

Under Approach C, ``Case.lawyer_id`` is the firm's single bookkeeping owner
(see ``app.api.deps.firm_lawyer_id``), so a single column can no longer tell
us which lawyer's live PJUD "Mis Causas" list a ROL was seen in. This M:N
association records "lawyer L has seen ROL R in their live list" — a ROL can
be sourced by more than one lawyer's account (e.g. the firm-wide scraper AND
an individual extension). Written by ``IngestService.ingest_cases`` /
``ingest_movements`` on every sync; consumed by ``get_pending_detail`` to keep
per-syncing-lawyer detail-rotation working once cases are firm-owned.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseLawyerSource(Base):
    """Records that ``lawyer_id`` has seen ``case_id`` in their live case list."""

    __tablename__ = "case_lawyer_source"

    __table_args__ = (
        UniqueConstraint("case_id", "lawyer_id", name="uq_case_lawyer_source_case_lawyer"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False, index=True)

    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    case = relationship("Case")
    lawyer = relationship("Lawyer")
