"""Daily job: detect candidate hitos from PJUD movements for the CURRENT period.

Runnable as ``python -m app.jobs.detect_hitos``. Scheduling is external (a VM
crontab, like the daily calendar email) — this module wires NO scheduler.

Reads the already-scraped ``movements`` + ``documents`` (no PJUD scraping, no
Shape), classifies favorable resolutions, and creates hitos in estado
``sugerido`` (origen=detector) for the admin (Carla) to confirm. Targets TODAY's
period in America/Santiago; closed periods are skipped by the service.
"""
import argparse
import logging

from app.core.database import SessionLocal
from app.services.deadline_engine import _today_chile
from app.services.hito_detector import HitoDetectorService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Detector de hitos desde PJUD.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Shadow-run: NO crea hitos, solo reporta cuántos/cuáles se crearían.",
    )
    parser.add_argument("--periodo", help="YYYY-MM (por defecto: mes actual en Chile).")
    args = parser.parse_args()

    periodo = args.periodo or f"{_today_chile():%Y-%m}"
    db = SessionLocal()
    try:
        resumen = HitoDetectorService(db).detectar(periodo, dry_run=args.dry_run)
        etiqueta = "SHADOW-RUN (dry)" if args.dry_run else "detector de hitos"
        logger.info("%s: %s", etiqueta, resumen)
    finally:
        db.close()


if __name__ == "__main__":
    main()
