"""PjudRequestCount — hourly footprint of the scraping station on PJUD.

One row per (UTC hour, account, egress IP, action, kind). ``kind`` is
``"request"`` for a request the station sent, or a rejection type
(``shape_challenge``, ``http_403``, ``http_429``, ``session_blocked``,
``connection_reset``). ``count`` accumulates across flushes of the same bucket.

``lawyer_id`` is deliberately a plain nullable integer (no FK): the metric must
survive a lawyer being removed and is NULL for requests with no account.
``egress_ip`` is ``"unknown"`` when the egress guard has no IP cached.

Retention: rows are tiny (about 13 accounts x a few actions x 24 hours a day) and
no purge is needed for now.
"""

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from app.core.database import Base


class PjudRequestCount(Base):
    __tablename__ = "pjud_request_counts"
    __table_args__ = (
        UniqueConstraint(
            "hour_utc", "lawyer_id", "egress_ip", "action", "kind",
            name="uq_pjud_request_counts_bucket",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    hour_utc = Column(DateTime, nullable=False, index=True)  # naive UTC, truncated to the hour
    lawyer_id = Column(Integer, nullable=True)
    egress_ip = Column(String(45), nullable=False)
    action = Column(String(32), nullable=False)
    kind = Column(String(32), nullable=False)
    count = Column(Integer, nullable=False, default=0)
