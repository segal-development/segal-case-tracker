"""BusquedaFechaRun — one daily "Búsqueda por Fecha" run per (fecha, corte)."""

from sqlalchemy import JSON, Column, Date, DateTime, Integer, String, UniqueConstraint

from app.core.database import Base


class BusquedaFechaRun(Base):
    __tablename__ = "pjud_busqueda_fecha_runs"
    __table_args__ = (UniqueConstraint("fecha", "corte", name="uq_pjud_busqueda_fecha_runs_fecha_corte"),)

    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(Date, nullable=False)
    corte = Column(String(255), nullable=False)
    # running | done | partial | failed
    status = Column(String(16), nullable=False)
    contadores = Column(JSON, nullable=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
