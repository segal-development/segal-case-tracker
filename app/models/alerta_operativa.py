"""Registro de las alertas operativas que el sistema envió.

No es una bitácora decorativa: es lo que permite avisar una sola vez. Una
alerta que se repite en cada ciclo del worker se convierte en ruido, y el
ruido se ignora — exactamente lo que pasó con ``sync_history``, donde el dato
de 29 corridas fallidas estaba y nadie lo miraba.

Se guarda una fila por aviso enviado, no una por tipo: saber cuántas veces y
cuándo avisamos es tan útil como saber que avisamos.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String

from app.core.database import Base


class AlertaOperativa(Base):
    """Un aviso operativo efectivamente enviado."""

    __tablename__ = "alertas_operativas"

    __table_args__ = (
        # La consulta del cooldown es siempre "el último aviso de este tipo".
        Index("ix_alertas_operativas_tipo_enviada", "tipo", "enviada_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    #: Qué se avisó, p. ej. ``"scraping_detenido"``.
    tipo = Column(String(50), nullable=False)
    #: Estado que motivó el aviso, para poder leer la historia sin adivinar.
    estado = Column(String(20), nullable=True)
    #: Texto humano del motivo, tal como viajó en el correo.
    detalle = Column(String(500), nullable=True)
    enviada_at = Column(DateTime, nullable=False, default=datetime.utcnow)
