"""Document model - Downloaded case documents."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, BigInteger, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Document(Base):
    """Document downloaded from PJUD and stored in GCS."""

    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id"), nullable=False)
    movement_id = Column(Integer, ForeignKey("movements.id"), nullable=True)
    # Slice 1: optional FK to the CaseEscrito that originated this cert document
    escrito_id = Column(Integer, ForeignKey("case_escritos.id"), nullable=True)

    # Document info (filename nullable: download has not happened at persist time)
    filename = Column(String(255), nullable=True)
    content_type = Column(String(100), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)

    # Storage
    gcs_path = Column(String(512), nullable=True)   # GCS bucket path
    pjud_url = Column(String(1024), nullable=True)  # Original PJUD URL

    # PJUD token fields (Slice 1)
    doc_type = Column(String(50), nullable=True)           # e.g. "resolution", "cert_envio"
    pjud_endpoint = Column(String(255), nullable=True)     # e.g. "documentos/docuS.php"
    pjud_token = Column(Text, nullable=True)               # Live JWT, refreshed each sync
    pjud_token_hash = Column(String(64), nullable=True, index=True)
    # ^ Stable identity hash: sha256(doc_type|case_rol|scope_key) — NOT the JWT hash.
    # Unique partial index (WHERE pjud_token_hash IS NOT NULL) added in migration 006.

    # Status: pending → (download) → stored | failed | unavailable
    status = Column(String(20), nullable=False, server_default="pending")

    # Timestamps
    document_date = Column(DateTime, nullable=True)  # Date on document
    downloaded_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    case = relationship("Case", back_populates="documents")
    escrito = relationship("CaseEscrito", backref="documents")
