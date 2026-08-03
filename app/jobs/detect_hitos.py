"""Daily job: detect candidate hitos from PJUD movements for the CURRENT period.

Runnable as ``python -m app.jobs.detect_hitos``. Scheduling is external (a VM
crontab, like the daily calendar email) — this module wires NO scheduler.

Reads the already-scraped ``movements`` + ``documents`` (no PJUD scraping, no
Shape), classifies favorable resolutions, and creates hitos in estado
``sugerido`` (origen=detector) for the admin (Carla) to confirm. Targets TODAY's
period in America/Santiago; closed periods are skipped by the service.
"""
import logging

from app.core.database import SessionLocal
from app.services.deadline_engine import _today_chile
from app.services.hito_detector import HitoDetectorService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    periodo = f"{_today_chile():%Y-%m}"
    db = SessionLocal()
    try:
        resumen = HitoDetectorService(db).detectar(periodo)
        logger.info("detector de hitos: %s", resumen)
    finally:
        db.close()


if __name__ == "__main__":
    main()
