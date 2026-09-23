"""Cartera snapshot models — the frozen monthly portfolio for tramitación KPIs.

"Cartera del mes = snapshot del día 1 a las 00:00 hrs": every tramitación KPI
is computed against a FROZEN monthly portfolio, never against the live
``cases`` table. See ``app.services.cartera_snapshot`` for how a snapshot is
taken and read.
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from app.core.database import Base


class CarteraSnapshot(Base):
    """One row per civil causa, frozen at the moment ``tomar_snapshot`` ran.

    ``lawyer_id`` and ``nivel`` are the RESOLVED owner (asignación override >
    litigante of record > nothing — see
    ``app.services.lawyer_roster.resolved_owner_by_case``) and their nivel,
    both frozen here — never re-joined against the live ``lawyers`` table
    when reading a past period, so a later reassignment or nivel change can
    never rewrite history. ``last_movement_at`` / ``last_detail_checked_at``
    are the freshness evidence this row's KPIs rest on, frozen the same way.
    """

    __tablename__ = "cartera_snapshots"
    __table_args__ = (
        UniqueConstraint("periodo", "case_id", name="uq_cartera_snapshots_periodo_case"),
        Index("ix_cartera_snapshots_periodo_lawyer", "periodo", "lawyer_id"),
        Index("ix_cartera_snapshots_periodo_matriz", "periodo", "matriz"),
    )

    id = Column(Integer, primary_key=True, index=True)
    periodo = Column(String(7), nullable=False, index=True)  # "YYYY-MM"
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True)
    nivel = Column(String(10), nullable=True)  # frozen at snapshot time

    matriz = Column(String(20), nullable=True)
    matriz_etapa = Column(String(80), nullable=True)
    matriz_origen = Column(String(30), nullable=True)

    # Freshness evidence, frozen with the row — see module docstring.
    last_movement_at = Column(DateTime, nullable=True)
    last_detail_checked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CarteraSnapshotRun(Base):
    """One row per period a snapshot was taken for.

    The idempotency/audit log ``tomar_snapshot`` checks before rebuilding a
    period: a run already existing for ``periodo`` means "don't retake it"
    unless the caller explicitly asks for ``reemplazar=True``.
    """

    __tablename__ = "cartera_snapshot_runs"

    id = Column(Integer, primary_key=True, index=True)
    periodo = Column(String(7), unique=True, nullable=False, index=True)
    tomado_at = Column(DateTime, nullable=False)
    causas = Column(Integer, nullable=False)
    # RUT of the admin who requested it; NULL when the scheduler took it
    # automatically.
    tomado_por = Column(String(20), nullable=True)
