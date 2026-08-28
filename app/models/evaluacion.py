"""Staff evaluation module (evaluación de procuradores/personas).

Self-administrable replica of the firm's Google Form that rates a person on a
set of 1-5 criteria plus a free-text comment. Everything is admin-configurable:

- ``EvaluacionCriterio``  — the criteria being scored (add/edit/soft-remove).
- ``EvaluacionEvaluable`` — the curated list of who can be evaluated (any role).
- ``Evaluacion``         — one submitted evaluation (evaluado + evaluador).
- ``EvaluacionRespuesta`` — one score per criterio inside an evaluation.

Additive and isolated: no existing table is touched. A ``puntaje`` of NULL means
"No aplica" (N/A) and is only valid for criteria whose ``permite_na`` is True.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class EvaluacionCriterio(Base):
    """Admin-configured evaluation criterion (a single 1-5 scored line)."""

    __tablename__ = "evaluacion_criterios"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(255), nullable=False)
    # Free-label group, e.g. "Criterios" or "Adicionales".
    grupo = Column(String(100), nullable=False, server_default="Criterios", default="Criterios")
    orden = Column(Integer, nullable=False, server_default=text("0"), default=0)
    # Whether "No aplica" (a NULL score) is an accepted answer for this criterion.
    permite_na = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    # Soft-delete flag: inactive criteria stay for historical responses.
    activo = Column(Boolean, nullable=False, server_default=text("true"), default=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class EvaluacionEvaluable(Base):
    """A firm person (any role) who the admin has enabled to be evaluated."""

    __tablename__ = "evaluacion_evaluables"

    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(
        Integer, ForeignKey("lawyers.id"), nullable=False, unique=True, index=True
    )
    activo = Column(Boolean, nullable=False, server_default=text("true"), default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    lawyer = relationship("Lawyer")


class Evaluacion(Base):
    """One submitted evaluation of ``evaluado`` by ``evaluador``."""

    __tablename__ = "evaluaciones"

    id = Column(Integer, primary_key=True, index=True)
    evaluado_lawyer_id = Column(
        Integer, ForeignKey("lawyers.id"), nullable=False, index=True
    )
    # Public form: the evaluator is NOT authenticated — identified by the email
    # they type in (like the original Google Form's email field).
    evaluador_email = Column(String(255), nullable=False, index=True)
    # "YYYY-MM" — the month the evaluation belongs to. Backs the monthly limit
    # (1 evaluation per evaluador+evaluado+mes) enforced in the endpoint.
    periodo = Column(String(7), nullable=False, index=True)
    comentarios = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    evaluado = relationship("Lawyer", foreign_keys=[evaluado_lawyer_id])
    respuestas = relationship(
        "EvaluacionRespuesta",
        back_populates="evaluacion",
        cascade="all, delete-orphan",
    )


class EvaluacionRespuesta(Base):
    """A single criterion score inside an evaluation.

    ``puntaje`` is an integer 1..5, or NULL for "No aplica" (only allowed when
    the criterion's ``permite_na`` is True).
    """

    __tablename__ = "evaluacion_respuestas"

    id = Column(Integer, primary_key=True, index=True)
    evaluacion_id = Column(
        Integer,
        ForeignKey("evaluaciones.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    criterio_id = Column(
        Integer, ForeignKey("evaluacion_criterios.id"), nullable=False, index=True
    )
    puntaje = Column(Integer, nullable=True)  # 1..5, or NULL = "No aplica"

    evaluacion = relationship("Evaluacion", back_populates="respuestas")
    criterio = relationship("EvaluacionCriterio")
