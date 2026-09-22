"""Shared (non per-lawyer) public hito-form links.

The per-lawyer form link lives on ``lawyers.hito_form_token``. This table holds
the GENERIC links, keyed by ``kind``: today only ``procuradores`` — one shared
token the firm's assistants use to submit a hito on behalf of the lawyer they
pick from a select. ``token`` NULL = revoked / never issued.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.core.database import Base

FORM_LINK_KIND_PROCURADORES = "procuradores"


class HitoFormLink(Base):
    __tablename__ = "hito_form_links"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(40), unique=True, nullable=False)
    token = Column(String(64), unique=True, nullable=True)  # never logged
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
