"""PublicLinkToken — secret link tokens for no-login pages. Only the SHA-256 hash is stored."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base

PURPOSE_DEMANDAS_PUBLICAS = "demandas_publicas"


class PublicLinkToken(Base):
    __tablename__ = "public_link_tokens"

    id = Column(Integer, primary_key=True, index=True)
    purpose = Column(String(64), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("lawyers.id"), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
