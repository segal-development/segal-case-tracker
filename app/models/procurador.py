"""Procurador -> abogado links.

A procurador (``lawyers.role = 'procurador'``) supports 1 or 2 firm lawyers: his case
scope is the union of theirs and he may enter hitos on their behalf. One row per link.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer

from app.core.database import Base


class ProcuradorAbogado(Base):
    __tablename__ = "procurador_abogados"

    procurador_id = Column(Integer, ForeignKey("lawyers.id"), primary_key=True)
    abogado_id = Column(Integer, ForeignKey("lawyers.id"), primary_key=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
