"""Job: raise and send the plazo semaforo alerts due now.

Runnable as ``python -m app.jobs.send_semaforo_alerts``. Idempotent and cheap
(one scan of the active excepciones / ratificacion plazos), so schedule it as
often as the lateness you can tolerate: the sync cycle already runs it once per
cycle, but plazos discovered DURING a multi-hour cycle only get their first
alert at the next one. Scheduling is external (a VM crontab); this module
intentionally wires NO scheduler.
"""

import logging

from app.core.database import SessionLocal
from app.services.semaforo_alerts import run_semaforo_alerts

logger = logging.getLogger(__name__)


def main() -> None:
    # Logging setup belongs to the entry point, not the module (see
    # send_daily_calendars).
    logging.basicConfig(level=logging.INFO)

    db = SessionLocal()
    try:
        summary = run_semaforo_alerts(db)
        logger.info("semaforo de plazos: %s", summary)
    finally:
        db.close()


if __name__ == "__main__":
    main()
