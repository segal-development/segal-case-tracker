"""Daily job: email each firm lawyer their calendar guide for TODAY.

Runnable as ``python -m app.jobs.send_daily_calendars``. Scheduling is handled
externally (a VM crontab that fires at 07:00 America/Santiago) — this module
intentionally wires NO scheduler.

Targets TODAY in America/Santiago (``_today_chile()``): sent at 7 AM, the guide
tells each lawyer what they have for the current day, plus the "Requiere
atención" novedades of the last few days.
"""

import logging

from app.core.database import SessionLocal
from app.services.daily_agenda import send_daily_calendar_emails
from app.services.deadline_engine import _today_chile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    target_day = _today_chile()
    db = SessionLocal()
    try:
        summary = send_daily_calendar_emails(db, target_day)
        logger.info("daily calendar emails: %s", summary)
    finally:
        db.close()


if __name__ == "__main__":
    main()
