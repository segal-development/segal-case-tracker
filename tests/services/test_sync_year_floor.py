"""SyncService.sync_cases must never CREATE a case older than DETAIL_MIN_YEAR.

The firm only works cases from 2021 onward. The browser-extension path
(ingest_service) already filters, but the local worker syncs through
SyncService.sync_cases — which was still creating pre-2021 rows, re-ingesting
exactly the cases the cleanup removed. These tests pin the write-time year
floor (fail-open on non-standard ROLs; existing rows still update).
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Force all models to register with Base before create_all.
from app.main import app as _app  # noqa: F401 — side-effect import
from app.config import settings
from app.core.database import Base
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court
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
                created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    sqlite_db.add(lw)
    sqlite_db.commit()
    return lw


def _scraped(**kw):
    d = dict(rol="C-1234-2025", tribunal="24º Juzgado Civil de Santiago",
             caratulado="BANCO/DEUDOR", fecha_ingreso="01/01/2025",
             estado_cuaderno="Tramitación", cuaderno="Principal")
    d.update(kw)
    return ScrapedCase(**d)


def test_pre_min_year_case_is_not_created(sqlite_db, lawyer):
    assert settings.DETAIL_MIN_YEAR == 2021  # guard: the floor these tests assume
    result = SyncService(sqlite_db).sync_cases(
        lawyer_id=lawyer.id,
        scraped_cases=[_scraped(rol="C-999-2020")],
        competencia="civil",
    )
    assert sqlite_db.query(Case).count() == 0
    assert result.cases_new == 0


def test_min_year_and_newer_cases_are_created(sqlite_db, lawyer):
    result = SyncService(sqlite_db).sync_cases(
        lawyer_id=lawyer.id,
        scraped_cases=[_scraped(rol="C-1-2021"), _scraped(rol="C-2-2025")],
        competencia="civil",
    )
    assert result.cases_new == 2
    assert {c.rol for c in sqlite_db.query(Case).all()} == {"C-1-2021", "C-2-2025"}


def test_mixed_batch_keeps_only_recent(sqlite_db, lawyer):
    SyncService(sqlite_db).sync_cases(
        lawyer_id=lawyer.id,
        scraped_cases=[_scraped(rol="C-1-2018"), _scraped(rol="C-2-2023"),
                       _scraped(rol="C-3-2005")],
        competencia="civil",
    )
    assert {c.rol for c in sqlite_db.query(Case).all()} == {"C-2-2023"}


def test_non_standard_rol_is_kept_fail_open(sqlite_db, lawyer):
    """A ROL without a real 4-digit year suffix must NOT be silently dropped."""
    SyncService(sqlite_db).sync_cases(
        lawyer_id=lawyer.id,
        scraped_cases=[_scraped(rol="EXH-55"), _scraped(rol="C-7-SINANIO")],
        competencia="civil",
    )
    assert sqlite_db.query(Case).count() == 2


def test_existing_pre_min_year_case_still_updates(sqlite_db, lawyer):
    """The floor only blocks CREATION — an already-stored old case still syncs
    (updates), so the skip never strands a pre-existing row."""
    court = Court(code="24CIV", name="24º Juzgado Civil de Santiago",
                  region="RM", type="civil")
    sqlite_db.add(court)
    sqlite_db.commit()
    old = Case(lawyer_id=lawyer.id, court_id=court.id, rol="C-500-2019",
               plaintiff="X", defendant="Y", competencia="civil",
               created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    sqlite_db.add(old)
    sqlite_db.commit()

    result = SyncService(sqlite_db).sync_cases(
        lawyer_id=lawyer.id,
        scraped_cases=[_scraped(rol="C-500-2019", caratulado="BANCO/NUEVO")],
        competencia="civil",
    )
    assert result.cases_updated == 1
    assert sqlite_db.query(Case).count() == 1
