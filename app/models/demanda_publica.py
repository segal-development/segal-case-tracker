"""DemandaPublica — civil causa found via PJUD's public "Búsqueda por Fecha" with no demandado lawyer.

Only the extracted demand text is stored, never the PDF. UNIQUE(rol, tribunal).
Deliberately has NO FK to ``cases`` (only the optional ``courts``).
"""

from datetime import datetime

from sqlalchemy import JSON, Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.core.database import Base


class DemandaPublica(Base):
    __tablename__ = "pjud_demandas_publicas"
    __table_args__ = (UniqueConstraint("rol", "tribunal", name="uq_pjud_demandas_publicas_rol_tribunal"),)

    id = Column(Integer, primary_key=True, index=True)
    rol = Column(String(64), nullable=False)
    tribunal = Column(String(255), nullable=False)
    court_id = Column(Integer, ForeignKey("courts.id"), nullable=True)
    corte = Column(String(255), nullable=True)
    caratulado = Column(Text, nullable=False, default="")
    fecha_ingreso = Column(Date, nullable=True, index=True)
    texto_demanda = Column(Text, nullable=True)
    litigantes = Column(JSON, nullable=False, default=list)
    encontrada_at = Column(DateTime, nullable=False, default=datetime.utcnow)
