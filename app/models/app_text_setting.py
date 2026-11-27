"""AppTextSetting — server-side settings whose value is text (``goals`` is integer-only).

Read/written through ``app.services.app_settings.get_text`` / ``set_text``.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.core.database import Base


class AppTextSetting(Base):
    __tablename__ = "app_text_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
