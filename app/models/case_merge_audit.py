"""CaseMergeAudit model - loser->winner case_id mapping (ADR-008 Slice 2).

Records every duplicate-ROL merge performed by the data migration that
collapses per-lawyer duplicate Case rows into one firm-owned Case
(``app.services.case_merge.run_case_merge``). This is the reversibility
ledger required by the "Data migration invariants" spec requirement: for
every merge, the loser->winner ``case_id`` mapping (plus the loser's former
``lawyer_id`` and ``rol``) must be recoverable even after the loser Case row
is deleted.

``loser_case_id`` is intentionally NOT a foreign key: the row it refers to is
deleted by the same migration that writes this audit entry, so a FK
constraint would either block the deletion or require ON DELETE semantics
that would erase the very id we need to keep. ``winner_case_id`` survives the
migration and IS a real FK.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseMergeAudit(Base):
    """One loser->winner Case merge event, for audit and best-effort rollback."""

    __tablename__ = "case_merge_audit"

    id = Column(Integer, primary_key=True, index=True)

    # Not a ForeignKey on purpose — the loser Case row no longer exists after
    # the migration deletes it. See module docstring.
    loser_case_id = Column(Integer, nullable=False, index=True)
    winner_case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)
    loser_lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)

    rol = Column(String(50), nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    winner_case = relationship("Case")
    loser_lawyer = relationship("Lawyer")
