"""CasePriority — a lawyer's personal "follow this causa with priority" mark.

One row per (case, lawyer). Marks are personal: each lawyer only sees and changes
their own. The cap per lawyer (20) is enforced by the API, not the schema.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint, func

from app.core.database import Base


class CasePriority(Base):
    __tablename__ = "case_priority"
    __table_args__ = (
        UniqueConstraint("case_id", "lawyer_id", name="uq_case_priority_case_lawyer"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    lawyer_id = Column(
        Integer, ForeignKey("lawyers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(DateTime, nullable=False, server_default=func.now())
