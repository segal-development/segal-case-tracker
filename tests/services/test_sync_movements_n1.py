"""Regression guard for the N+1 query fix in SyncService.sync_movements.

Before the fix, ``sync_movements`` called ``_upsert_movement`` per scraped
movement, and ``_upsert_movement`` ran ONE
``db.query(Movement).filter(case_id==, folio==, description==).first()`` per
movement (a case with 28 movements issued 28 SELECTs against ``movements``).
It now prefetches ALL of the case's existing movements in ONE query into a
``{(folio, description): Movement}`` dict and passes it as
``_upsert_movement(..., existing_by_key=...)``, which does an in-memory lookup.

These tests lock in that behavior:
  * query-scaling: SELECTs against the ``movements`` table stay a small
    constant as N grows, on BOTH the all-insert first pass and the all-existing
    re-sync pass (compare N=10 vs N=50 — must be identical, not O(N));
  * correctness: first sync inserts N rows (new_count == N), re-sync inserts 0
    (new_count == 0), total rows == N, no duplicates;
  * a duplicate (folio, description) within one scraped list resolves to a
    single row (exercises the in-loop ``existing_by_key[key] = movement``).

The query-counting technique mirrors ``tests/services/test_sync_case_upsert_n1.py``
exactly: an in-memory SQLite engine with a ``before_cursor_execute`` listener
that counts SELECTs whose FROM clause references the ``movements`` table. Only
Movement-table SELECTs are counted, so the one-off Case/Lawyer/Webhook lookups
``sync_movements`` hoists up front do not confuse the assertion. SQLite may
emit a different query shape than Postgres, so the assertions target the
SCALING (constant vs O(N)), not a Postgres-exact count.

Notifications are silenced by passing an exhausted ``NotifyBudget(0)``: alerts
are still persisted, but no email/webhook DISPATCH (NotificationService) runs.
"""

import re
import pytest
from datetime import datetime
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Force all models to register with Base before create_all.
from app.main import app as _app  # noqa: F401 — side-effect import

from app.core.database import Base
from app.models.lawyer import Lawyer
from app.models.case import Case
from app.models.court import Court
from app.models.movement import Movement

from app.services.sync_service import SyncService, ScrapedMovement, NotifyBudget


# ---------------------------------------------------------------------------
# Fixtures — mirror test_sync_case_upsert_n1.py
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
def case(sqlite_db):
    """Seed a Lawyer + Court + Case and return the persisted Case row."""
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
    db.flush()

    court = Court(code="C1", name="1º Juzgado Civil", region="RM", type="civil")
    db.add(court)
    db.flush()

    case = Case(
        lawyer_id=lawyer.id,
        court_id=court.id,
        rol="C-1234-2025",
        competencia="civil",
        status="active",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(case)
    db.commit()
    return case


def _scraped_movement(i: int) -> ScrapedMovement:
    """Build a valid ScrapedMovement with a natural key — (folio, descripcion)
    — that is unique per ``i``."""
    return ScrapedMovement(
        folio=str(i),
        fecha="01/01/2025",
        tipo_tramite="Resolución",
        descripcion=f"Movimiento {i}",
        etapa="Cuaderno Principal",
    )


@contextmanager
def count_selects(engine, tables):
    """Count SELECT statements issued against any of ``tables`` while the
    context manager is active. Returns a mutable dict {"count": int}."""
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


# The Movement table (see app/models/movement.py). ONLY SELECTs referencing
# this table are counted, so sync_movements' hoisted Case/Lawyer/Webhook
# lookups do not affect the assertion.
_TABLE = ["movements"]


def _exhausted_budget() -> NotifyBudget:
    """A NotifyBudget with no dispatch slots → alerts are persisted but no
    NotificationService email/webhook dispatch runs (keeps tests side-effect
    free and offline)."""
    return NotifyBudget(0)


# ---------------------------------------------------------------------------
# Query-count regression guard: O(1) SELECTs, not O(N)
# ---------------------------------------------------------------------------

class TestQueryCountBound:
    def test_movement_selects_do_not_scale_with_n(self, sqlite_db, case):
        """Syncing N movements must issue a constant number of SELECTs against
        the movements table (the single batch prefetch), not one per movement.
        Proven by comparing an N=10 run to an N=50 run and asserting the count
        does not grow with N."""
        small = [_scraped_movement(i) for i in range(10)]
        with count_selects(sqlite_db.get_bind(), _TABLE) as counts_small:
            SyncService(sqlite_db).sync_movements(
                case.id, small, budget=_exhausted_budget()
            )

        # A fresh set of movements (new natural keys) on the SAME case — all
        # inserts again — must not scale the SELECT count with N.
        large = [_scraped_movement(i) for i in range(1000, 1050)]  # N=50
        with count_selects(sqlite_db.get_bind(), _TABLE) as counts_large:
            SyncService(sqlite_db).sync_movements(
                case.id, large, budget=_exhausted_budget()
            )

        # Small constant, independent of N (the batch prefetch SELECT).
        assert counts_small["count"] <= 2
        assert counts_large["count"] <= 2
        # The core N+1 assertion: bigger batch, same query count.
        assert counts_large["count"] == counts_small["count"]

    def test_resync_selects_do_not_scale_with_n(self, sqlite_db, case):
        """Re-syncing the SAME N movements (now all existing → is_new=False)
        must ALSO stay a small constant, not O(N). The pre-fix code issued a
        SELECT per movement on this path too."""
        items = [_scraped_movement(i) for i in range(20)]

        # First pass: all inserts.
        SyncService(sqlite_db).sync_movements(
            case.id, items, budget=_exhausted_budget()
        )

        # Second pass: same natural keys → all existing.
        resync = [_scraped_movement(i) for i in range(20)]
        with count_selects(sqlite_db.get_bind(), _TABLE) as counts:
            SyncService(sqlite_db).sync_movements(
                case.id, resync, budget=_exhausted_budget()
            )

        # One batch prefetch SELECT for the whole re-sync pass — not N.
        assert counts["count"] <= 2

    def test_resync_creates_no_duplicate_rows(self, sqlite_db, case):
        """A re-sync with identical movements creates NO duplicate rows — the
        row count stays N."""
        items = [_scraped_movement(i) for i in range(20)]
        SyncService(sqlite_db).sync_movements(
            case.id, items, budget=_exhausted_budget()
        )

        resync = [_scraped_movement(i) for i in range(20)]
        SyncService(sqlite_db).sync_movements(
            case.id, resync, budget=_exhausted_budget()
        )

        assert (
            sqlite_db.query(Movement).filter(Movement.case_id == case.id).count()
            == 20
        )


# ---------------------------------------------------------------------------
# Correctness: batching did not change semantics
# ---------------------------------------------------------------------------

class TestUpsertCorrectnessUnchanged:
    def test_first_sync_inserts_all_rows(self, sqlite_db, case):
        items = [_scraped_movement(i) for i in range(20)]

        new_count, _alert_count = SyncService(sqlite_db).sync_movements(
            case.id, items, budget=_exhausted_budget()
        )

        assert new_count == 20
        assert (
            sqlite_db.query(Movement).filter(Movement.case_id == case.id).count()
            == 20
        )

    def test_resync_inserts_zero(self, sqlite_db, case):
        items = [_scraped_movement(i) for i in range(20)]
        SyncService(sqlite_db).sync_movements(
            case.id, items, budget=_exhausted_budget()
        )

        resync = [_scraped_movement(i) for i in range(20)]
        new_count, _alert_count = SyncService(sqlite_db).sync_movements(
            case.id, resync, budget=_exhausted_budget()
        )

        assert new_count == 0
        assert (
            sqlite_db.query(Movement).filter(Movement.case_id == case.id).count()
            == 20
        )


# ---------------------------------------------------------------------------
# Duplicate (folio, description) within a single scraped list
# ---------------------------------------------------------------------------

class TestDuplicateKeyWithinBatch:
    def test_duplicate_key_in_one_list_resolves_to_one_row(self, sqlite_db, case):
        """Two scraped movements with the SAME (folio, description) in a single
        list must produce ONE row — the in-loop ``existing_by_key[key] = movement``
        update makes the second item resolve to the row the first just inserted,
        with no duplicate INSERT."""
        dupe = [
            _scraped_movement(7),
            _scraped_movement(7),  # same (folio, description)
        ]

        new_count, _alert_count = SyncService(sqlite_db).sync_movements(
            case.id, dupe, budget=_exhausted_budget()
        )

        assert new_count == 1
        assert (
            sqlite_db.query(Movement).filter(Movement.case_id == case.id).count()
            == 1
        )
