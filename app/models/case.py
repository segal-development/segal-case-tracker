"""Case model - Civil court cases."""

from datetime import datetime
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Index,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
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

    Asignación por nivel (``assigned_lawyer_id``): a THIRD, independent axis
    on top of the two above. ``CaseLitigante``/``resolve_case_scope`` reflect
    the LEGAL abogado-of-record (who PJUD says represents the firm on this
    causa); ``lawyer_id`` is the sync/provenance key described above. Neither
    tells the firm which INTERNAL lawyer (by nivel: junior/pleno/senior)
    currently works the causa day-to-day, which the firm needs to reassign
    freely when a causa's ``matriz`` (M1 Baja/M1 Alta/M2/M3) requires a
    different level than the assignee. ``assigned_lawyer_id`` is that
    override; see ``effective_lawyer_id`` and ``EFFECTIVE_LAWYER_ID`` below,
    and ``app.api.v1.asignacion`` for the endpoints that manage it.
    ``lawyer_id`` itself is NEVER written by that workflow — see the comment
    on ``existing_by_rol`` in ``app.services.sync_service`` for why.
    """

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("lawyer_id", "rol", name="uq_cases_lawyer_rol"),
        Index("ix_cases_assigned_lawyer_matriz", "assigned_lawyer_id", "matriz"),
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

    # Manual semáforo override (liberación de causa — auditor + dirección dual
    # sign-off). Pins the color to a chosen range until a NEWER movement
    # supersedes it (then the engine clears it and resumes computing).
    semaforo_override = Column(String(10), nullable=True)
    semaforo_override_at = Column(DateTime, nullable=True)
    semaforo_override_by = Column(String(255), nullable=True)

    # Prescripción (statute of limitations) — populated by DeadlineEngine.recompute_case
    titulo_tipo = Column(String(30), nullable=True)   # input: pagare|letra|cheque|escritura_publica|sentencia|otro
    titulo_fecha = Column(Date, nullable=True)         # input: title's key date
    prescripcion_cumplida = Column(Boolean, nullable=False, server_default=text("false"), default=False)  # computed
    prescripcion_fecha = Column(Date, nullable=True)   # computed: titulo_fecha + plazo

    # Decision engine — populated by DeadlineEngine.recompute_case (via DecisionEngine)
    recommended_action_code = Column(String(50), nullable=True)  # e.g. "oponer_excepciones"
    next_review_at = Column(Date, nullable=True)  # next date this case should be manually reviewed

    # Matriz de clasificación (M1 Baja/M1 Alta/M2/M3) — populated by
    # app.services.matriz_classifier.classify_case. Advisory/derived; never
    # blocks a sync. See that module's docstring for the precedence rules.
    matriz = Column(String(20), nullable=True, index=True)
    matriz_etapa = Column(String(80), nullable=True)
    matriz_origen = Column(String(30), nullable=True)  # pjud_procedimiento|pjud_etapa|sin_detalle|no_mapeada|error
    # Sysgal's business "procedimiento simple" (e.g. "Vive Tranquilo"). NULL
    # until Sysgal sends it; the classifier falls back to the default
    # "Juicio Ejecutivo Completo" map while it is NULL.
    matriz_proc_simple = Column(String(80), nullable=True)
    matriz_computed_at = Column(DateTime, nullable=True)

    # Asignación por nivel — override of WHO works this causa internally,
    # independent of ``lawyer_id`` (sync provenance, never touched here) and
    # of litigante-derived legal attribution. NULL means "not reassigned":
    # the effective lawyer is ``lawyer_id`` (see ``effective_lawyer_id``).
    # Deliberately NOT backfilled by the migration that introduced this
    # column — NULL carries real meaning (nobody has overridden it yet) and
    # keeps that migration instant on the full cases table.
    assigned_lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True, index=True)
    assigned_at = Column(DateTime, nullable=True)
    assigned_by_rut = Column(String(20), nullable=True)  # RUT of the admin who reassigned it
    assigned_motivo = Column(String(255), nullable=True)

    # Timestamps
    filed_at = Column(DateTime, nullable=True)  # Fecha de ingreso
    last_movement_at = Column(DateTime, nullable=True)
    last_detail_checked_at = Column(DateTime, nullable=True)  # Rotation tracking
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    lawyer = relationship("Lawyer", back_populates="cases", foreign_keys=[lawyer_id])
    assigned_lawyer = relationship("Lawyer", foreign_keys=[assigned_lawyer_id])
    client = relationship("Client", back_populates="cases")
    court = relationship("Court", back_populates="cases")
    movements = relationship("Movement", back_populates="case", order_by="desc(Movement.movement_date)")
    documents = relationship("Document", back_populates="case")
    generated_documents = relationship("GeneratedDocument", back_populates="case")
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

    @property
    def effective_lawyer_id(self) -> int:
        """The lawyer who currently works this causa, for business reads.

        ``lawyer_id`` is the sync/provenance key (see ``existing_by_rol`` in
        ``app.services.sync_service`` — it MUST stay untouched by
        reassignment or the next sync of the original lawyer's PJUD account
        would not find this causa and would re-create it as a duplicate).
        ``assigned_lawyer_id`` is an optional admin override (asignación por
        nivel, ``app.api.v1.asignacion``). NULL means nobody has overridden
        it, so the effective lawyer falls back to ``lawyer_id`` — this makes
        every business read safe to switch to ``effective_lawyer_id`` today,
        since it is currently a no-op (no row has ``assigned_lawyer_id`` set)
        and becomes correct the moment a reassignment happens.
        """
        return self.assigned_lawyer_id if self.assigned_lawyer_id is not None else self.lawyer_id


# SQL-level equivalent of ``Case.effective_lawyer_id``, for use in queries
# (filters, joins, group-by) so no call site hand-rolls the COALESCE.
EFFECTIVE_LAWYER_ID = func.coalesce(Case.assigned_lawyer_id, Case.lawyer_id)
