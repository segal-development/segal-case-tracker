"""sync_cases records the syncing lawyer's sighting in case_lawyer_source (T3a).

Purely additive: Case.lawyer_id and the existing dedup are untouched. The
sighting is written in bulk (one SELECT per chunk), never per scraped case.
"""

import re
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.main import app as _app  # noqa: F401 — registers all models with Base
from app.core.database import Base
from app.models.case import Case
from app.models.case_lawyer_source import CaseLawyerSource
from app.models.lawyer import Lawyer
from app.services.sync_service import ScrapedCase, SyncService


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sqlite_db(sqlite_engine):
    session = sessionmaker(bind=sqlite_engine)()
    try:
        yield session
    finally:
        session.close()


def _lawyer(db, rut):
    lawyer = Lawyer(
        rut=rut,
        name=f"Abogado {rut}",
        email=f"{rut}@segal.cl",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.commit()
    return lawyer


def _scraped(rol):
    return ScrapedCase(
        rol=rol,
        tribunal="1º Juzgado Civil de Santiago",
        caratulado="BANCO ITAU/FERNANDEZ",
        fecha_ingreso="01/01/2025",
        estado_cuaderno="Tramitación",
        cuaderno="Principal",
    )


@contextmanager
def count_selects(engine, table):
    pattern = re.compile(r"\bFROM\s+" + re.escape(table) + r"\b", re.IGNORECASE)
    counters = {"count": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        s = statement.strip()
        if s[:6].upper() == "SELECT" and pattern.search(s):
            counters["count"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield counters
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


def _sources(db, lawyer_id):
    return (
        db.query(CaseLawyerSource)
        .filter(CaseLawyerSource.lawyer_id == lawyer_id)
        .order_by(CaseLawyerSource.case_id)
        .all()
    )


class TestSyncRecordsSighting:
    def test_new_cases_get_a_source_row_for_the_syncing_lawyer(self, sqlite_db):
        lawyer = _lawyer(sqlite_db, "11111111-1")

        SyncService(sqlite_db).sync_cases(
            lawyer.id, [_scraped("C-1-2025"), _scraped("C-2-2025")], "civil"
        )

        cases = sqlite_db.query(Case).order_by(Case.id).all()
        rows = _sources(sqlite_db, lawyer.id)
        assert [r.case_id for r in rows] == [c.id for c in cases]
        assert all(c.lawyer_id == lawyer.id for c in cases)  # ownership unchanged
        assert all(r.first_seen_at and r.last_seen_at for r in rows)

    def test_resync_does_not_duplicate_and_refreshes_last_seen(self, sqlite_db):
        lawyer = _lawyer(sqlite_db, "22222222-2")
        sync = SyncService(sqlite_db)
        sync.sync_cases(lawyer.id, [_scraped("C-1-2025")], "civil")
        (first,) = _sources(sqlite_db, lawyer.id)
        old = datetime.utcnow() - timedelta(days=3)
        first.first_seen_at = old
        first.last_seen_at = old
        sqlite_db.commit()

        sync.sync_cases(lawyer.id, [_scraped("C-1-2025")], "civil")

        sqlite_db.expire_all()
        (row,) = _sources(sqlite_db, lawyer.id)
        assert row.id == first.id
        assert row.first_seen_at == old
        assert row.last_seen_at > old

    def test_two_lawyers_same_rol_each_get_their_own_row(self, sqlite_db):
        l1 = _lawyer(sqlite_db, "33333333-3")
        l2 = _lawyer(sqlite_db, "44444444-4")
        sync = SyncService(sqlite_db)

        sync.sync_cases(l1.id, [_scraped("C-9-2025")], "civil")
        sync.sync_cases(l2.id, [_scraped("C-9-2025")], "civil")

        c1 = sqlite_db.query(Case).filter(Case.lawyer_id == l1.id).one()
        c2 = sqlite_db.query(Case).filter(Case.lawyer_id == l2.id).one()
        assert [r.case_id for r in _sources(sqlite_db, l1.id)] == [c1.id]
        assert [r.case_id for r in _sources(sqlite_db, l2.id)] == [c2.id]

    def test_year_skipped_case_gets_no_row(self, sqlite_db):
        lawyer = _lawyer(sqlite_db, "55555555-5")

        SyncService(sqlite_db).sync_cases(lawyer.id, [_scraped("C-1-2010")], "civil")

        assert sqlite_db.query(Case).count() == 0
        assert _sources(sqlite_db, lawyer.id) == []


class TestSightingQueryCountBound:
    def _selects(self, engine, db, n):
        lawyer = _lawyer(db, f"9000{n:04d}-1")
        scraped = [_scraped(f"C-{i}-2025") for i in range(1, n + 1)]
        with count_selects(engine, "case_lawyer_source") as counter:
            SyncService(db).sync_cases(lawyer.id, scraped, "civil")
        assert len(_sources(db, lawyer.id)) == n
        return counter["count"]

    def test_selects_do_not_scale_with_scraped_cases(self, sqlite_engine, sqlite_db):
        small = self._selects(sqlite_engine, sqlite_db, 10)
        large = self._selects(sqlite_engine, sqlite_db, 50)
        assert small == large
