"""SysgalUsuario — Sysgal lawyer accounts and their mapping to our ``Lawyer``."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.database import Base


class SysgalUsuario(Base):
    __tablename__ = "sysgal_usuarios"

    id = Column(Integer, primary_key=True, index=True)
    sysgal_id = Column(Integer, nullable=False, unique=True, index=True)
    nombre = Column(String(255), nullable=False)
    perfil = Column(String(100), nullable=True)
    estado = Column(String(50), nullable=True)
    correo = Column(String(255), nullable=True)

    # The mapping, set by a human (PUT /sysgal/usuarios/{id}); never auto-applied.
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True, index=True)
    mapped_by = Column(String(20), nullable=True)  # RUT of whoever set the mapping
    synced_at = Column(DateTime, nullable=False, default=datetime.utcnow)
