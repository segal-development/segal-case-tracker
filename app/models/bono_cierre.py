"""Monthly bonus period close (payroll freeze).

Once Dirección Jurídica closes a período, its bonus variables and the hitos that
feed that month become read-only, so a paid liquidación can't change after the
fact. A closed período can be reopened by an admin (audited). A período is
"closed" when a row exists here with estado == "cerrado".
"""
from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime

from app.core.database import Base

CIERRE_CERRADO = "cerrado"
CIERRE_ABIERTO = "abierto"


class BonoCierre(Base):
    __tablename__ = "bono_cierres"

    id = Column(Integer, primary_key=True, index=True)
    periodo = Column(String(7), nullable=False, unique=True, index=True)  # "YYYY-MM"
    estado = Column(String(10), nullable=False, default=CIERRE_CERRADO)

    cerrado_by_rut = Column(String(20), nullable=True)
    cerrado_by_name = Column(String(255), nullable=True)
    cerrado_at = Column(DateTime, nullable=True)

    reabierto_by_rut = Column(String(20), nullable=True)
    reabierto_by_name = Column(String(255), nullable=True)
    reabierto_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
