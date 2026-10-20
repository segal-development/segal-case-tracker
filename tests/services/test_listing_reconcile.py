"""Every case the listing produced must be accounted for after a sync.

The PJUD listing reports more cases than the database holds. Part of the gap
is cuadernos (the listing counts rows, not causas) and part is the deliberate
pre-2021 floor, but ~830 cases across four lawyers stay unexplained, and their
caratulados are the firm's own clients.

The year floor `continue`d without counting, so `cases_new + cases_updated <
cases_total` had no interpretation. These tests pin that side of the ledger.

NOTE: `cases_total` is `len(scraped_cases)` -- what the SCRAPER already
brought, not what PJUD said existed. The gap this chases lives one layer up,
between the listing's reported total and the rows pagination collects; that
is reconciled in the scraper (see test_listing_pagination_reconcile.py).
"""

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app as _app  # noqa: F401 — side-effect import
from app.core.database import Base
from app.models.lawyer import Lawyer
from app.services.sync_service import SyncService, ScrapedCase


@pytest.fixture()
def sqlite_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def lawyer(sqlite_db):
    lw = Lawyer(rut="12345678-9", name="Abogada Test", email="t@segal.cl", is_active=True,
                created_at=datetime.utcnow())
    sqlite_db.add(lw)
    sqlite_db.commit()
    return lw


def _scraped(rol: str) -> ScrapedCase:
    return ScrapedCase(
        rol=rol, tribunal="1º Juzgado Civil de Santiago", caratulado="BANCO X/PEREZ",
        fecha_ingreso="01/03/2025", estado_cuaderno="Tramitación", cuaderno="Principal",
    )


class TestListingReconcile:
    def test_the_year_floor_skips_are_counted_not_silent(self, sqlite_db, lawyer):
        result = SyncService(sqlite_db).sync_cases(
            lawyer_id=lawyer.id,
            scraped_cases=[_scraped("C-1-2020"), _scraped("C-2-2019"), _scraped("C-3-2025")],
            competencia="civil",
        )
        assert result.cases_new == 1
        assert result.cases_year_skipped == 2

    def test_everything_adds_up_when_nothing_is_lost(self, sqlite_db, lawyer):
        result = SyncService(sqlite_db).sync_cases(
            lawyer_id=lawyer.id,
            scraped_cases=[_scraped("C-1-2025"), _scraped("C-2-2024"), _scraped("C-3-2020")],
            competencia="civil",
        )
        contado = (result.cases_new + result.cases_updated
                   + result.cases_year_skipped + len(result.errors))
        assert contado == result.cases_total
        assert result.cases_unaccounted == 0

    def test_the_reconciliation_is_logged(self, sqlite_db, lawyer):
        with patch("app.services.sync_service.logger") as log:
            SyncService(sqlite_db).sync_cases(
                lawyer_id=lawyer.id,
                scraped_cases=[_scraped("C-1-2025"), _scraped("C-2-2019")],
                competencia="civil",
            )
        linea = "\n".join(str(c) for c in log.info.call_args_list)
        assert "LISTING_RECONCILE" in linea
