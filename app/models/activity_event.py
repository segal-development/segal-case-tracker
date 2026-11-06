"""ActivityEvent model: who saw and did what, visible to admins.

SECURITY: this table NEVER stores request/response bodies, query strings, headers
(other than a trimmed User-Agent), tokens, passwords or keys. The only free-form
inputs are the route template, the path, the frontend path of a view and, for
failed logins, the submitted email/RUT (``identifier``).
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, func

from app.core.database import Base


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_events_lawyer_created", "lawyer_id", "created_at"),)

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=func.now(), index=True
    )
    lawyer_id = Column(Integer, ForeignKey("lawyers.id"), nullable=True)
    actor_name = Column(String(255), nullable=True)
    actor_role = Column(String(20), nullable=True)
    # "view" | "action" | "login" | "login_failed"
    kind = Column(String(20), nullable=False)
    method = Column(String(10), nullable=True)
    route = Column(String(255), nullable=False)
    path = Column(String(255), nullable=True)
    status_code = Column(Integer, nullable=True)
    case_id = Column(Integer, nullable=True, index=True)
    target = Column(String(255), nullable=True)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)
    identifier = Column(String(120), nullable=True)
