"""Hitos (milestone → bonus) models.

The firm tracks lawyer "hitos" (procedural milestones) that feed a monthly bonus
(the "SISTEMA DE HITOS"). This replaces a manual Google Sheet: parameters are
pre-loaded (HitoTipo), each hito is entered with a MANDATORY PJUD evidence
capture, and an admin approves it. Business rule (firm's): "Evidencia
obligatoria: captura PJUD del escrito presentado · PJUD prevalece sobre CRM ·
sin evidencia no se paga" — a hito can NEVER be approved without evidence.
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class HitoTipo(Base):
    """Catalog of hito types + their gross value (pre-loaded from PARÁMETROS).

    Editable config: the money value is snapshotted onto each Hito at creation,
    so changing a type's value never retro-alters already-approved hitos.
    """

    __tablename__ = "hito_tipos"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)  # e.g. "pleno_prescripcion"
    label = Column(String(255), nullable=False)  # e.g. "Prescripción terminada (sentencia firme)"
    nivel = Column(String(10), nullable=False)  # "junior" | "pleno"
    valor_bruto = Column(Integer, nullable=False)  # CLP gross
    etapa_tramite = Column(String(255), nullable=True)  # "ETAPA + TRAMITE Sysgal" descriptor
    verificacion = Column(String(500), nullable=True)  # required PJUD verification
    activo = Column(Boolean, nullable=False, server_default=text("true"), default=True)
    orden = Column(Integer, nullable=False, server_default=text("0"), default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Hito approval states.
HITO_SUGERIDO = "sugerido"   # auto-detected candidate, awaiting admin confirmation
HITO_PENDIENTE = "pendiente"
HITO_APROBADO = "aprobado"
HITO_RECHAZADO = "rechazado"

# Provenance of a hito row.
ORIGEN_MANUAL = "manual"      # hand-entered (form or Excel import)
ORIGEN_DETECTOR = "detector"  # created by the PJUD hito detector


class Hito(Base):
    """A recorded milestone earned by a lawyer, pending admin approval.

    ``valor_bruto`` is snapshotted from the HitoTipo at creation. A hito with no
    ``evidencia_storage_key`` can never be approved (firm rule: sin evidencia no
    se paga). The month it counts toward the bonus is ``fecha_hito``'s month.
    """

    __tablename__ = "hitos"

    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False, index=True)  # abogado AT
    hito_tipo_id = Column(Integer, ForeignKey("hito_tipos.id"), nullable=False, index=True)
    valor_bruto = Column(Integer, nullable=False)  # snapshot from HitoTipo at creation

    fecha_hito = Column(Date, nullable=False, index=True)  # when the milestone occurred
    rol_causa = Column(String(50), nullable=True)  # the case ROL / RUT reference
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=True, index=True)  # linked case, if matched
    procedimiento = Column(String(100), nullable=True)  # RECURSO, EXHIBICION, ...
    descripcion = Column(String(500), nullable=True)  # free note ("DOCUMENTACIÓN OK", ...)
    # ETAPA/TRAMITE — manual for now; later auto-filled from scraped movements.
    etapa_sysgal = Column(String(100), nullable=True)
    tramite_sysgal = Column(String(255), nullable=True)

    # PJUD evidence capture (mandatory for approval).
    evidencia_storage_key = Column(String(512), nullable=True)
    evidencia_filename = Column(String(255), nullable=True)
    evidencia_content_type = Column(String(100), nullable=True)

    estado = Column(String(20), nullable=False, server_default=HITO_PENDIENTE, default=HITO_PENDIENTE)

    # Detector provenance. For manual hitos: origen='manual', the rest NULL. For
    # auto-detected candidates: origen='detector' + the movement that triggered it,
    # the rule that fired, and the classifier confidence (alta|media|baja).
    origen = Column(String(20), nullable=False, server_default=ORIGEN_MANUAL, default=ORIGEN_MANUAL)
    movement_id = Column(Integer, ForeignKey("movements.id"), nullable=True, index=True)
    regla_code = Column(String(50), nullable=True)
    confianza = Column(String(10), nullable=True)

    created_by_rut = Column(String(20), nullable=True)
    created_by_name = Column(String(255), nullable=True)
    aprobado_by_rut = Column(String(20), nullable=True)
    aprobado_by_name = Column(String(255), nullable=True)
    aprobado_at = Column(DateTime, nullable=True)
    rechazo_motivo = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lawyer = relationship("Lawyer")
    tipo = relationship("HitoTipo")

    @property
    def tiene_evidencia(self) -> bool:
        return bool(self.evidencia_storage_key)
