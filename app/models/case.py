"""Case model - Civil court cases."""

from datetime import datetime
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class Case(Base):
    """Civil court case tracked by the firm.

    Approach C (ADR-007, ``unificar-modelo-causas``): ``lawyer_id`` is the
    firm's single canonical bookkeeping owner (resolved via
    ``app.api.deps.firm_lawyer_id`` / ``FIRM_LAWYER_RUT``), NOT the
    individual lawyer working the case. Per-lawyer attribution is derived
    from ``CaseLitigante`` rows instead (see ``resolve_case_scope`` /
    ``case_ids_for_abogado``), so a lawyer sees a case iff they are an
    abogado-of-record litigante on it — not by matching ``lawyer_id``.

    ``uq_cases_lawyer_rol`` below therefore now effectively enforces ONE
    canonical ``Case`` per ROL under the firm's ``lawyer_id`` (it degenerates
    from "one case per (lawyer, rol) pair" to "one case per rol", since every
    row shares the same firm ``lawyer_id``). This prevents duplicate `Case`
    rows for the same ROL being created for different lawyers, which is the
    exact bug Approach C's migration (``case_merge`` / migration 024)
    resolves for pre-existing data.
    """

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("lawyer_id", "rol", name="uq_cases_lawyer_rol"),
    )

    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    court_id = Column(Integer, ForeignKey("courts.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    
    # Case identification
    rol = Column(String(50), nullable=False, index=True)  # C-1234-2024
    rit = Column(String(50), nullable=True)  # Internal PJUD ID
    competencia = Column(String(20), default="civil")  # civil, laboral, penal
    
    # Parties
    plaintiff = Column(String(500), nullable=True)  # Demandante
    defendant = Column(String(500), nullable=True)  # Demandado
    
    # Case details
    matter = Column(String(255), nullable=True)  # Materia (Cobro de pesos, etc.)
    procedure = Column(String(255), nullable=True)  # Procedimiento
    status = Column(String(50), default="active")  # active, archived, closed
    
    # PJUD specific
    pjud_causa_id = Column(String(100), nullable=True)  # PJUD internal ID
    
    # Procedural deadline engine — populated by DeadlineEngine.recompute_case
    procedural_state = Column(String(30), nullable=True)
    semaforo = Column(String(10), nullable=True)
    next_deadline_at = Column(Date, nullable=True)
    abandono_disponible = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    next_deadline_fatal = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    en_apremio = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    consulta_reserved = Column(Boolean, nullable=False, server_default=text("false"), default=False)  # True when consulta_by_rol returns None (case not in public consulta)

    # Prescripción (statute of limitations) — populated by DeadlineEngine.recompute_case
    titulo_tipo = Column(String(30), nullable=True)   # input: pagare|letra|cheque|escritura_publica|sentencia|otro
    titulo_fecha = Column(Date, nullable=True)         # input: title's key date
    prescripcion_cumplida = Column(Boolean, nullable=False, server_default=text("false"), default=False)  # computed
    prescripcion_fecha = Column(Date, nullable=True)   # computed: titulo_fecha + plazo

    # Decision engine — populated by DeadlineEngine.recompute_case (via DecisionEngine)
    recommended_action_code = Column(String(50), nullable=True)  # e.g. "oponer_excepciones"
    next_review_at = Column(Date, nullable=True)  # next date this case should be manually reviewed

    # Timestamps
    filed_at = Column(DateTime, nullable=True)  # Fecha de ingreso
    last_movement_at = Column(DateTime, nullable=True)
    last_detail_checked_at = Column(DateTime, nullable=True)  # Rotation tracking
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    lawyer = relationship("Lawyer", back_populates="cases")
    client = relationship("Client", back_populates="cases")
    court = relationship("Court", back_populates="cases")
    movements = relationship("Movement", back_populates="case", order_by="desc(Movement.movement_date)")
    documents = relationship("Document", back_populates="case")
    alerts = relationship("Alert", back_populates="case")
    # Case-detail entity back-refs (populated from PJUD tabs)
    litigantes = relationship("CaseLitigante", back_populates="case")
    notificaciones = relationship("CaseNotificacion", back_populates="case")
    escritos = relationship("CaseEscrito", back_populates="case")
    exhortos = relationship("CaseExhorto", back_populates="case")
    deadlines = relationship(
        "CaseDeadline",
        back_populates="case",
        cascade="all, delete-orphan",
    )
