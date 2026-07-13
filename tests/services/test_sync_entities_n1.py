"""Regression guard for the N+1 query fix in ``_sync_entities``.

Before the fix, ``_sync_entities`` ran ONE
``db.query(spec.model).filter(case_id==, natural_key==key).first()`` per
scraped item (a case with 52 entities issued 52 SELECTs against the entity
table) plus a ``db.flush()`` per updated item. It now issues ONE batch SELECT
for all of the case's existing rows up front, keyed by ``natural_key``, does
in-memory lookups in the loop, and flushes once after the loop.

These tests lock in that behavior:
  * query-scaling: SELECTs against the entity table stay a small constant as N
    grows, on BOTH the all-insert first pass and the all-update second pass;
  * correctness: insert (is_new=True) / update-in-place (is_new=False), no
    duplicates, updatable fields persisted;
  * a duplicate natural_key within one scraped_list resolves to a single row.

The query-counting technique mirrors
``tests/services/test_sync_case_upsert_n1.py`` exactly: an in-memory SQLite
engine with a ``before_cursor_execute`` listener that counts SELECTs whose
FROM clause references the entity table. SQLite may emit a slightly different
query shape than Postgres, so the assertions target the SCALING (constant vs
O(N)) — comparing a small-N run against a large-N run — not a Postgres-exact
count.
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
from app.models.case_notificacion import CaseNotificacion

from app.scrapper.pjud.base import PJUDNotificacion
from app.services.sync_service import (
    _sync_entities,
    SPEC_NOTIFICACION,
    notificacion_natural_key,
)


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


def _scraped_notificacion(i: int, estado: str = "Notificada") -> PJUDNotificacion:
    """Build a valid scraped PJUDNotificacion whose natural_key is unique per
    ``i`` (via ``nombre``). ``estado_notif`` is an updatable field EXCLUDED from
    the natural_key, so changing it re-targets the SAME row (update path)."""
    return PJUDNotificacion(
        rol="1",
        estado_notif=estado,
        tipo_notif="Personal",
        fecha_tramite="01/01/2025",
        tipo_participante="Demandado",
        nombre=f"PERSON {i}",
        tramite="Notificación demanda",
        obs_fallida="",
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


_TABLE = ["case_notificaciones"]


# ---------------------------------------------------------------------------
# Query-count regression guard: O(1) SELECTs, not O(N)
# ---------------------------------------------------------------------------

class TestQueryCountBound:
    def test_entity_selects_do_not_scale_with_n(self, sqlite_db, case):
        """Syncing N entities must issue a constant number of SELECTs against
        the entity table (ideally exactly 1 batch existence SELECT), not one
        per item. Proven by comparing a small-N run to a much larger-N run and
        asserting the count does not grow with N."""
        small = [_scraped_notificacion(i) for i in range(10)]
        with count_selects(sqlite_db.get_bind(), _TABLE) as counts_small:
            _sync_entities(sqlite_db, case.id, small, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # A brand-new set of items (fresh natural_keys) on the SAME case — all
        # inserts again — must not scale the SELECT count with N.
        large = [_scraped_notificacion(i) for i in range(1000, 1050)]  # N=50
        with count_selects(sqlite_db.get_bind(), _TABLE) as counts_large:
            _sync_entities(sqlite_db, case.id, large, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # Small constant, independent of N (the batch existence SELECT).
        assert counts_small["count"] <= 2
        assert counts_large["count"] <= 2
        # The core N+1 assertion: bigger batch, same query count.
        assert counts_large["count"] == counts_small["count"]

    def test_update_path_selects_do_not_scale_with_n(self, sqlite_db, case):
        """Re-syncing the SAME N items (now all existing → update path) must
        ALSO stay a small constant, not O(N). The pre-fix code flushed per
        updated item, re-introducing the round-trips this guards against."""
        items = [_scraped_notificacion(i) for i in range(20)]

        # First pass: all inserts.
        _sync_entities(sqlite_db, case.id, items, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # Second pass: same natural_keys, changed updatable field → all updates.
        changed = [_scraped_notificacion(i, estado="Fallida") for i in range(20)]
        with count_selects(sqlite_db.get_bind(), _TABLE) as counts:
            _sync_entities(sqlite_db, case.id, changed, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # One batch existence SELECT for the whole update pass — not N.
        assert counts["count"] <= 2


# ---------------------------------------------------------------------------
# Correctness: batching did not change semantics
# ---------------------------------------------------------------------------

class TestUpsertCorrectnessUnchanged:
    def test_first_sync_inserts_all_rows_is_new_true(self, sqlite_db, case):
        items = [_scraped_notificacion(i) for i in range(20)]

        results = _sync_entities(sqlite_db, case.id, items, SPEC_NOTIFICACION)
        sqlite_db.commit()

        assert len(results) == 20
        assert all(is_new for _row, is_new in results)
        assert (
            sqlite_db.query(CaseNotificacion)
            .filter(CaseNotificacion.case_id == case.id)
            .count()
            == 20
        )

    def test_resync_updates_in_place_no_duplicates(self, sqlite_db, case):
        items = [_scraped_notificacion(i) for i in range(20)]
        _sync_entities(sqlite_db, case.id, items, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # Same natural_keys, changed updatable field.
        changed = [_scraped_notificacion(i, estado="Fallida") for i in range(20)]
        results = _sync_entities(sqlite_db, case.id, changed, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # All updates, no inserts.
        assert len(results) == 20
        assert all(not is_new for _row, is_new in results)
        # Row count stayed N — no duplicates.
        assert (
            sqlite_db.query(CaseNotificacion)
            .filter(CaseNotificacion.case_id == case.id)
            .count()
            == 20
        )

    def test_updatable_field_new_value_persisted(self, sqlite_db, case):
        items = [_scraped_notificacion(0, estado="Notificada")]
        _sync_entities(sqlite_db, case.id, items, SPEC_NOTIFICACION)
        sqlite_db.commit()

        key = notificacion_natural_key(items[0])
        before = (
            sqlite_db.query(CaseNotificacion)
            .filter(CaseNotificacion.natural_key == key)
            .one()
        )
        assert before.estado_notif == "Notificada"

        # Re-sync with a changed updatable field (natural_key unchanged).
        _sync_entities(
            sqlite_db, case.id, [_scraped_notificacion(0, estado="Fallida")], SPEC_NOTIFICACION
        )
        sqlite_db.commit()

        after = (
            sqlite_db.query(CaseNotificacion)
            .filter(CaseNotificacion.natural_key == key)
            .one()
        )
        assert after.id == before.id
        assert after.estado_notif == "Fallida"


# ---------------------------------------------------------------------------
# Duplicate natural_key within a single scraped_list
# ---------------------------------------------------------------------------

class TestDuplicateKeyWithinBatch:
    def test_duplicate_natural_key_in_one_list_resolves_to_one_row(self, sqlite_db, case):
        """Two scraped items with the SAME natural_key in a single list must
        produce ONE row — the in-loop ``existing_by_key[key] = row`` map update
        makes the second item resolve to the row the first just inserted, with
        no duplicate INSERT and no unique-constraint crash."""
        dupe = [
            _scraped_notificacion(0, estado="Notificada"),
            _scraped_notificacion(0, estado="Fallida"),  # same natural_key
        ]

        results = _sync_entities(sqlite_db, case.id, dupe, SPEC_NOTIFICACION)
        sqlite_db.commit()

        # First is an insert, second resolves to the same row (update).
        assert [is_new for _row, is_new in results] == [True, False]
        assert (
            sqlite_db.query(CaseNotificacion)
            .filter(CaseNotificacion.case_id == case.id)
            .count()
            == 1
        )
        row = (
            sqlite_db.query(CaseNotificacion)
            .filter(CaseNotificacion.case_id == case.id)
            .one()
        )
        # The second item's updatable value wins.
        assert row.estado_notif == "Fallida"
