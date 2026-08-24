"""Presentacion model - a queued filing (demanda/escrito) for the external Presentación API.

Persistence for the GEDOC escrito-upload integration: GEDOC posts a filing to
be presented at the PJUD's OJV; this row records the request and its lifecycle
state. Slice 1 is the API contract + persistence ONLY — a created presentación
just sits at ``estado="en_cola"``; NO browser automation, NO PJUD/Playwright
calls, and no background worker process it yet. Zero writes to any external
system.

The ``idempotency_key`` (GEDOC-provided) dedups repeat POSTs so a retry never
creates a duplicate filing. The ``payload`` JSON carries the litigantes and the
documents to attach (documento principal + anexos); the outcome columns
(``rol_asignado`` / ``ruc`` / ``numero_identificador`` / ``certificado_url``)
are filled later by the (future) worker once the filing is actually presented.
"""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base

# Lifecycle states: en_cola -> presentando -> cargada_pendiente_envio ->
# revisado -> envio_confirmado -> presentada / rechazada / error.
PRES_EN_COLA = "en_cola"
PRES_PRESENTANDO = "presentando"
PRES_CARGADA_PENDIENTE = "cargada_pendiente_envio"
PRES_REVISADO = "revisado"
PRES_ENVIO_CONFIRMADO = "envio_confirmado"
PRES_PRESENTADA = "presentada"
PRES_RECHAZADA = "rechazada"
PRES_ERROR = "error"


class Presentacion(Base):
    """A filing queued for presentation at the OJV. Isolated from the internal app."""

    __tablename__ = "presentaciones"

    id = Column(Integer, primary_key=True, index=True)
    # GEDOC-provided idempotency key — a repeat POST returns the existing row.
    idempotency_key = Column(String(255), unique=True, index=True, nullable=False)
    api_key_id = Column(
        Integer, ForeignKey("presentacion_api_keys.id"), nullable=True
    )

    # tipo_gestion: which OJV flow — "demanda" | "escrito".
    tipo_gestion = Column(String(50), nullable=False)
    # modo: "semiauto" (carga y deja en Bandeja para que un humano envíe) |
    # "auto" (envía y trae comprobante).
    modo = Column(String(20), nullable=False, default="semiauto")

    competencia = Column(String(100), nullable=True)
    corte = Column(String(255), nullable=True)
    tribunal = Column(String(255), nullable=True)
    rol = Column(String(50), nullable=True)  # for escrito on an existing causa
    anio = Column(String(4), nullable=True)
    procedimiento = Column(String(255), nullable=True)
    materia = Column(String(255), nullable=True)
    dirigido_a_rut = Column(String(20), nullable=True)  # abogado "Dirigido a"
    # RUT of the OJV account (lawyer) that presents; Segal custodia la 2ª clave.
    credential_ref = Column(String(20), nullable=False)

    # {litigantes:[...], documento_principal:{url,referencia},
    #  documentos:[{url,referencia,tipo,cantidad,original_papel}]}
    payload = Column(JSON, nullable=False)

    estado = Column(String(30), nullable=False, default=PRES_EN_COLA)

    # Outcome fields — filled later by the (future) worker, never in Slice 1.
    rol_asignado = Column(String(50), nullable=True)
    ruc = Column(String(50), nullable=True)
    numero_identificador = Column(String(50), nullable=True)  # folio del comprobante
    fecha_envio = Column(DateTime, nullable=True)
    certificado_url = Column(String(1024), nullable=True)
    error_detail = Column(String(1024), nullable=True)
    intentos = Column(Integer, nullable=False, default=0)

    # Audit trail of the cross-reviewer (revisión cruzada / four-eyes) — a person
    # DISTINCT from the sender, supplied by GEDOC — recorded when the filing moves
    # to ``revisado``.
    revisado_por = Column(String(255), nullable=True)  # who cross-reviewed
    revisado_at = Column(DateTime, nullable=True)

    # Audit trail of who authorized the irreversible send (the redactor, via
    # GEDOC), recorded when the filing moves to ``envio_confirmado``.
    confirmado_por = Column(String(255), nullable=True)  # who confirmed the send
    confirmado_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
