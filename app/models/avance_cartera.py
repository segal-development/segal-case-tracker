"""AvanceCarteraSnapshot — frozen monthly copy of Sysgal's cartera statistics.

One row per período ``YYYY-MM``. ``payload`` is the full Sysgal response exactly as
received (never recomputed); the bono V3 reads each lawyer's ``cobertura`` from it.
"""

from datetime import datetime

from sqlalchemy import JSON, Column, Date, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class AvanceCarteraSnapshot(Base):
    __tablename__ = "avance_cartera_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    periodo = Column(String(7), nullable=False, unique=True, index=True)  # "YYYY-MM"
    desde = Column(Date, nullable=False)
    hasta = Column(Date, nullable=False)
    taken_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    payload = Column(JSON, nullable=False)
    taken_by = Column(Integer, ForeignKey("lawyers.id"), nullable=True)  # NULL = the scheduler
