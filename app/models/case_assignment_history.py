"""CaseAssignmentHistory — who owned a case before each assignment write.

One row per case per batch (traspaso de cartera or manual reasignar). It keeps the
previous override columns exactly as stored (NULL included) so a batch can be
reverted to the prior state, and ``from_lawyer_id`` keeps the RESOLVED owner
before the move for reporting.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class CaseAssignmentHistory(Base):
    __tablename__ = "case_assignment_history"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(36), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    from_lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True)
    prev_assigned_lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True)
    prev_assigned_at = Column(DateTime, nullable=True)
    prev_assigned_by_rut = Column(String(20), nullable=True)
    prev_assigned_motivo = Column(String(255), nullable=True)

    to_lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    motivo = Column(String(255), nullable=True)
    created_by_rut = Column(String(20), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reverted_at = Column(DateTime, nullable=True)
