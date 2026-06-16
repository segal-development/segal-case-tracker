"""CaseDeadline model — procedural deadline rows for civil cases."""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Date,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class CaseDeadline(Base):
    """One computed legal deadline for a civil case.

    Multiple rows per case (one per active DeadlineType).  The UNIQUE
    constraint on (case_id, deadline_type, triggered_at) prevents duplicate
    upserts across recompute runs and across REBELDÍA re-triggers.
    """

    __tablename__ = "case_deadlines"

    __table_args__ = (
        UniqueConstraint(
            "case_id",
            "deadline_type",
            "triggered_at",
            name="uq_case_deadline_type_triggered",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    deadline_type = Column(String(50), nullable=False)
    legal_basis = Column(String(100), nullable=True)
    due_date = Column(Date, nullable=False)

    # Date that started this deadline's countdown (movement_date or computed date).
    triggered_at = Column(Date, nullable=False)

    # active | met | expired | superseded
    status = Column(String(20), nullable=False, default="active")

    # Optional: the movement that triggered this deadline (nullable for computed triggers).
    source_movement_id = Column(
        Integer, ForeignKey("movements.id"), nullable=True
    )

    computed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    case = relationship("Case", back_populates="deadlines")
    source_movement = relationship("Movement")
