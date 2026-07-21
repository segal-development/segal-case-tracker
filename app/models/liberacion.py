"""Liberación de causa — a request to manually move a case's semáforo.

The assigned lawyer requests moving a case out of its current risk color (e.g.
crítico → atención/al día) because of a tramitación circumstance (negociación,
gestión). It only takes effect under DUAL sign-off — an auditor AND the dirección
(admin) must both authorize (cross-control: nobody downgrades a critical case
alone). On apply, a manual semáforo override is written on the Case; it holds
until a newer movement supersedes it. Reassigning the case to another lawyer is a
SEPARATE action (not this).
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.core.database import Base

LIB_PENDIENTE = "pendiente"
LIB_APLICADO = "aplicado"
LIB_RECHAZADO = "rechazado"


class LiberacionRequest(Base):
    __tablename__ = "liberacion_requests"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False, index=True)

    requested_by_rut = Column(String(20), nullable=True)
    requested_by_name = Column(String(255), nullable=True)
    target_semaforo = Column(String(10), nullable=False)  # rojo | amarillo | verde
    motivo = Column(String(500), nullable=True)  # negociación / gestión — the circumstance

    estado = Column(String(20), nullable=False, default=LIB_PENDIENTE)  # pendiente|aplicado|rechazado

    # Dual sign-off — cross-control. Both must be set before the move applies.
    auditor_aprobado_by_rut = Column(String(20), nullable=True)
    auditor_aprobado_by_name = Column(String(255), nullable=True)
    auditor_aprobado_at = Column(DateTime, nullable=True)
    direccion_aprobado_by_rut = Column(String(20), nullable=True)
    direccion_aprobado_by_name = Column(String(255), nullable=True)
    direccion_aprobado_at = Column(DateTime, nullable=True)

    aplicado_at = Column(DateTime, nullable=True)
    rechazado_by_rut = Column(String(20), nullable=True)
    rechazado_by_name = Column(String(255), nullable=True)
    rechazo_motivo = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    case = relationship("Case")

    @property
    def auditor_ok(self) -> bool:
        return self.auditor_aprobado_at is not None

    @property
    def direccion_ok(self) -> bool:
        return self.direccion_aprobado_at is not None
