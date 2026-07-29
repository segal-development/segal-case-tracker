"""Daily job: email each firm lawyer their next-day calendar guide.

Runnable as ``python -m app.jobs.send_daily_calendars``. Scheduling is handled
externally (a VM crontab) — this module intentionally wires NO scheduler.

Targets TOMORROW in America/Santiago (``_today_chile() + 1 day``): the guide
tells each lawyer what they have coming up the next day.
"""

import logging
from datetime import timedelta

from app.core.database import SessionLocal
from app.services.daily_agenda import send_daily_calendar_emails
from app.services.deadline_engine import _today_chile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    target_day = _today_chile() + timedelta(days=1)
    db = SessionLocal()
    try:
        summary = send_daily_calendar_emails(db, target_day)
        logger.info("daily calendar emails: %s", summary)
    finally:
        db.close()


if __name__ == "__main__":
    main()
