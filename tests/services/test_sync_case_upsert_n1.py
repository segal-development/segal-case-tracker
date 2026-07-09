"""
Tests for the N+1 query regression in SyncService.sync_cases / _upsert_case.

TDD: written BEFORE the fix — the query-count tests below must FAIL (RED) on
the current per-case-query implementation and PASS (GREEN) once:
  1. sync_cases pre-loads all existing cases for the lawyer in ONE query and
     passes them to _upsert_case instead of querying per case.
  2. _get_or_create_court caches courts in memory per SyncService instance
     instead of querying per case.

Bug context: for a lawyer with ~2085 cases, the old code issued ~2 SELECTs
per case (one for the case lookup, one for the court lookup) — ~4170 round
trips over the Cloud SQL Auth Proxy, which looked like a hang.
"""

import re
import pytest
from datetime import datetime
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Force all models to register with Base before create_all
from app.main import app as _app  # noqa: F401 — side-effect import

from app.core.database import Base
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court

from app.services.sync_service import SyncService, ScrapedCase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sqlite_engine():
    """A fresh in-memory SQLite engine per test (needed to attach the
    before_cursor_execute listener and to keep counts isolated)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def sqlite_db(sqlite_engine):
    Session = sessionmaker(bind=sqlite_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def lawyer(sqlite_db):
    db = sqlite_db
    lawyer = Lawyer(
        rut="12345678-9",
        name="Abogada Test",
        email="test@segal.cl",
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lawyer)
    db.commit()
    return lawyer


def _scraped_case(**kwargs):
    defaults = dict(
        rol="C-1234-2025",
        tribunal="24º Juzgado Civil de Santiago",
        caratulado="BANCO ITAU/FERNANDEZ",
        fecha_ingreso="01/01/2025",
        estado_cuaderno="Tramitación",
        cuaderno="Principal",
    )
    defaults.update(kwargs)
    return ScrapedCase(**defaults)


@contextmanager
def count_selects(engine, tables):
    """Count SELECT statements issued against any of `tables` while the
    context manager is active. Returns a mutable dict {"count": int}.
    """
    pattern = re.compile(
        r"\bFROM\s+(" + "|".join(re.escape(t) for t in tables) + r")\b",
        re.IGNORECASE,
    )
    counters = {"count": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        stripped = statement.strip()
        if stripped[:6].upper() == "SELECT" and pattern.search(stripped):
            counters["count"] += 1

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield counters
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


# ---------------------------------------------------------------------------
# Correctness: behavior unchanged after batching the reads
# ---------------------------------------------------------------------------

class TestUpsertCaseCorrectnessUnchanged:
    def test_insert_new_case_with_new_court(self, sqlite_db, lawyer):
        """A scraped case with no existing row and a brand-new court is
        created, and the court is created too."""
        sync = SyncService(sqlite_db)

        result = sync.sync_cases(
            lawyer_id=lawyer.id,
            scraped_cases=[_scraped_case(rol="C-1-2025", tribunal="1º Juzgado Civil de Santiago")],
            competencia="civil",
        )

        assert result.cases_new == 1
        assert result.cases_updated == 0

        case = sqlite_db.query(Case).filter(Case.rol == "C-1-2025").first()
        assert case is not None
        assert case.plaintiff == "BANCO ITAU"
        assert case.defendant == "FERNANDEZ"

        court = sqlite_db.query(Court).filter(Court.name == "1º Juzgado Civil de Santiago").first()
        assert court is not None
        assert case.court_id == court.id

    def test_update_existing_case_with_existing_court(self, sqlite_db, lawyer):
        """A scraped case matching an existing (lawyer_id, rol) row updates
        it in place instead of creating a duplicate."""
        court = Court(code="EXC1", name="Existing Court", region="RM", type="civil")
        sqlite_db.add(court)
        sqlite_db.flush()

        existing_case = Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol="C-2-2025",
            plaintiff="OLD PLAINTIFF",
            defendant="OLD DEFENDANT",
            status="active",
            competencia="civil",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        sqlite_db.add(existing_case)
        sqlite_db.commit()

        sync = SyncService(sqlite_db)
        result = sync.sync_cases(
            lawyer_id=lawyer.id,
            scraped_cases=[
                _scraped_case(
                    rol="c-2-2025",  # lowercase on the wire — must normalize to match
                    tribunal="Existing Court",
                    caratulado="NEW PLAINTIFF/NEW DEFENDANT",
                )
            ],
            competencia="civil",
        )

        assert result.cases_new == 0
        assert result.cases_updated == 1

        rows = sqlite_db.query(Case).filter(Case.rol == "C-2-2025").all()
        assert len(rows) == 1
        assert rows[0].id == existing_case.id
        assert rows[0].plaintiff == "NEW PLAINTIFF"
        assert rows[0].defendant == "NEW DEFENDANT"
        # Still pointed at the same (reused, not recreated) court.
        assert rows[0].court_id == court.id
        assert sqlite_db.query(Court).filter(Court.name == "Existing Court").count() == 1

    def test_existing_case_matched_by_rol_regardless_of_competencia_param(self, sqlite_db, lawyer):
        """Preserves the ORIGINAL lookup semantics: _upsert_case's pre-fix
        query filtered only by (lawyer_id, rol) — NOT by competencia. The
        batched pre-load must reproduce that exactly, otherwise a case
        synced under a different `competencia` value would be duplicated
        instead of updated."""
        court = Court(code="EXC2", name="Some Court", region="RM", type="laboral")
        sqlite_db.add(court)
        sqlite_db.flush()

        existing_case = Case(
            lawyer_id=lawyer.id,
            court_id=court.id,
            rol="T-9-2025",
            plaintiff="A",
            defendant="B",
            status="active",
            competencia="laboral",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        sqlite_db.add(existing_case)
        sqlite_db.commit()

        sync = SyncService(sqlite_db)
        result = sync.sync_cases(
            lawyer_id=lawyer.id,
            scraped_cases=[_scraped_case(rol="T-9-2025", tribunal="Some Court")],
            competencia="civil",  # different from the existing row's competencia
        )

        assert result.cases_new == 0
        assert result.cases_updated == 1
        assert sqlite_db.query(Case).filter(Case.rol == "T-9-2025").count() == 1


# ---------------------------------------------------------------------------
# Duplicate ROL within one batch
# ---------------------------------------------------------------------------

class TestDuplicateRolWithinBatch:
    def test_duplicate_rol_in_same_batch_creates_one_case(self, sqlite_db, lawyer):
        """Two scraped cases sharing the same normalized ROL (case
        differences included) in a single batch must not create two rows —
        the second one updates the row the first one just created."""
        sync = SyncService(sqlite_db)

        result = sync.sync_cases(
            lawyer_id=lawyer.id,
            scraped_cases=[
                _scraped_case(rol="C-3-2025", caratulado="FIRST/A"),
                _scraped_case(rol="c-3-2025", caratulado="SECOND/B"),
            ],
            competencia="civil",
        )

        assert result.cases_new == 1
        assert result.cases_updated == 1

        rows = sqlite_db.query(Case).filter(Case.rol == "C-3-2025").all()
        assert len(rows) == 1
        assert rows[0].plaintiff == "SECOND"
        assert rows[0].defendant == "B"


# ---------------------------------------------------------------------------
# Court reuse across cases
# ---------------------------------------------------------------------------

class TestCourtReuse:
    def test_shared_tribunal_creates_court_once(self, sqlite_db, lawyer):
        """N cases sharing one tribunal must create/look up the court
        exactly once, not N times."""
        sync = SyncService(sqlite_db)

        cases = [
            _scraped_case(rol=f"C-{i}-2025", tribunal="Shared Court")
            for i in range(5)
        ]

        with count_selects(sqlite_db.get_bind(), ["courts"]) as counts:
            sync.sync_cases(lawyer_id=lawyer.id, scraped_cases=cases, competencia="civil")

        assert sqlite_db.query(Court).filter(Court.name == "Shared Court").count() == 1
        # Exactly one SELECT against courts: the cache miss for the first
        # case. The other 4 must be served from the in-memory cache.
        assert counts["count"] == 1


# ---------------------------------------------------------------------------
# Query-count regression guard: O(1) SELECTs, not O(N)
# ---------------------------------------------------------------------------

class TestQueryCountBound:
    def test_case_and_court_selects_are_o1_not_on(self, sqlite_db, lawyer):
        """The actual regression guard: syncing N cases must issue a
        constant number of SELECTs against `cases` and `courts`, not one
        (or two) per case. Uses a handful of distinct tribunals repeated
        across many cases, mirroring real PJUD data where courts repeat
        heavily across a lawyer's caseload.
        """
        tribunales = ["Tribunal A", "Tribunal B", "Tribunal C"]

        def _batch(n, offset=0):
            return [
                _scraped_case(
                    rol=f"C-{offset + i}-2025",
                    tribunal=tribunales[i % len(tribunales)],
                )
                for i in range(n)
            ]

        sync_small = SyncService(sqlite_db)
        with count_selects(sqlite_db.get_bind(), ["cases", "courts"]) as counts_small:
            sync_small.sync_cases(lawyer_id=lawyer.id, scraped_cases=_batch(10), competencia="civil")

        # One pre-load SELECT for cases + up to 3 cache-miss SELECTs for the
        # 3 distinct courts = a small constant, independent of N.
        assert counts_small["count"] <= 4

        # A second, much larger batch (fresh ROLs, same 3 tribunales) must
        # NOT scale the SELECT count with N — proving O(1), not O(N).
        sync_large = SyncService(sqlite_db)
        with count_selects(sqlite_db.get_bind(), ["cases", "courts"]) as counts_large:
            sync_large.sync_cases(lawyer_id=lawyer.id, scraped_cases=_batch(50, offset=1000), competencia="civil")

        assert counts_large["count"] <= 4
        assert counts_large["count"] == counts_small["count"]
