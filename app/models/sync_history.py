"""SyncHistory model - Track synchronization history."""

from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship

from app.core.database import Base


class SyncHistory(Base):
    """Track sync operations for each lawyer/competency."""
    
    __tablename__ = "sync_history"
    
    id = Column(Integer, primary_key=True, index=True)
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=False)
    
    # What was synced
    competencia = Column(String(20), nullable=False)  # civil, laboral, penal
    year = Column(String(4), nullable=True)  # Filter year if any
    
    # Results
    cases_found = Column(Integer, default=0)
    cases_new = Column(Integer, default=0)
    cases_updated = Column(Integer, default=0)
    movements_new = Column(Integer, default=0)
    alerts_created = Column(Integer, default=0)
    
    # Status
    status = Column(String(20), default="completed")  # completed, failed, partial
    error_message = Column(String(1024), nullable=True)
    
    # Timing
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    
    # Metadata
    triggered_by = Column(String(20), default="manual")  # manual, scheduled, webhook
    
    # Relationships
    lawyer = relationship("Lawyer")
    
    def complete(self, status: str = "completed", error: str = None):
        """Mark sync as completed."""
        self.completed_at = datetime.utcnow()
        self.status = status
        self.error_message = error
        if self.started_at:
            self.duration_seconds = int((self.completed_at - self.started_at).total_seconds())
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lawyer_id": self.lawyer_id,
            "competencia": self.competencia,
            "year": self.year,
            "cases_found": self.cases_found,
            "cases_new": self.cases_new,
            "cases_updated": self.cases_updated,
            "movements_new": self.movements_new,
            "alerts_created": self.alerts_created,
            "status": self.status,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "triggered_by": self.triggered_by,
        }
