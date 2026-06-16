"""Integration tests for migration 008 round-trip.

Strategy
--------
Migrations 001-006 use inline FK constraints in op.add_column which SQLite
cannot apply via ALTER TABLE.  Running them in SQLite would require rewriting
those older migration files — not worth the churn for testing OUR migration.

Instead we:
 1. Build the minimal "007 state" schema directly with SQL (only the tables
    that migration 008 references: cases, movements, alembic_version).
 2. Stamp alembic_version at "007".
 3. Run command.upgrade(cfg, "008")  ← exercises only OUR migration.
 4. Assert the new table and columns exist.
 5. Run command.downgrade(cfg, "007") ← exercises only OUR downgrade.
 6. Assert the rollback is clean (pre-existing data survives).

This isolates the test to migration 008 behaviour only.
"""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).parent.parent.parent
ALEMBIC_DIR = REPO_ROOT / "alembic"
ALEMBIC_INI = ALEMBIC_DIR / "alembic.ini"

# Minimal DDL that mirrors the "007 state" for the tables migration 008 touches.
# Only includes columns that exist at revision 007 (no procedural_state etc.).
_SCHEMA_AT_007 = """
CREATE TABLE IF NOT EXISTS cases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lawyer_id        INTEGER NOT NULL DEFAULT 0,
    court_id         INTEGER NOT NULL DEFAULT 0,
    rol              TEXT    NOT NULL,
    competencia      TEXT    NOT NULL DEFAULT 'civil',
    status           TEXT    NOT NULL DEFAULT 'active',
    filed_at         TIMESTAMP,
    last_movement_at TIMESTAMP,
    last_detail_checked_at TIMESTAMP,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS movements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       INTEGER NOT NULL,
    stage         TEXT,
    procedure     TEXT,
    description   TEXT,
    movement_date TIMESTAMP,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num TEXT NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num) VALUES ('007');
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_db(tmp_path):
    """Fresh SQLite DB pre-seeded with the schema at revision 007.

    Returns (engine, url).
    """
    db_path = tmp_path / "test_migration_008.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        for stmt in _SCHEMA_AT_007.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
        conn.commit()
    yield engine, url
    engine.dispose()


@contextmanager
def _alembic_cfg(engine: Engine):
    """Config that injects an existing SQLite connection into env.py.

    env.py checks cfg.attributes["connection"] first, so it never tries to
    connect to the PostgreSQL URL from settings.DATABASE_URL.  render_as_batch
    is set in env.py so SQLite's drop_column limitation is handled via the
    copy-and-move strategy.
    """
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        yield cfg


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


# ---------------------------------------------------------------------------
# Migration file sanity tests (fast, no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMigration008Importable:
    """Verify migration file metadata before running any DB operations."""

    def _load(self):
        path = ALEMBIC_DIR / "versions" / "008_add_case_deadlines.py"
        spec = importlib.util.spec_from_file_location("migration_008", path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_file_exists(self) -> None:
        assert (ALEMBIC_DIR / "versions" / "008_add_case_deadlines.py").exists()

    def test_revision_id(self) -> None:
        assert self._load().revision == "008"

    def test_down_revision_chains_to_007(self) -> None:
        assert self._load().down_revision == "007"

    def test_upgrade_callable(self) -> None:
        assert callable(self._load().upgrade)

    def test_downgrade_callable(self) -> None:
        assert callable(self._load().downgrade)


# ---------------------------------------------------------------------------
# Round-trip tests (run only migration 008 against an SQLite fixture)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMigration008:
    def test_upgrade_007_to_008(self, sqlite_db) -> None:
        """Upgrade creates case_deadlines table and 3 new Case columns."""
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "008")

        assert "case_deadlines" in _tables(engine)
        case_cols = _columns(engine, "cases")
        assert "procedural_state" in case_cols
        assert "semaforo" in case_cols
        assert "next_deadline_at" in case_cols
        deadline_cols = _columns(engine, "case_deadlines")
        for col in ("case_id", "deadline_type", "due_date", "triggered_at", "status"):
            assert col in deadline_cols

    def test_downgrade_008_to_007(self, sqlite_db) -> None:
        """Downgrade drops case_deadlines and the 3 added columns; existing rows intact."""
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "008")
            # Insert a pre-existing case so we can verify data survival.
            with engine.connect() as conn:
                conn.execute(text(
                    "INSERT INTO cases (rol, competencia) VALUES ('C-TEST-008', 'civil')"
                ))
                conn.commit()
            command.downgrade(cfg, "007")

        assert "case_deadlines" not in _tables(engine)
        case_cols = _columns(engine, "cases")
        assert "procedural_state" not in case_cols
        assert "semaforo" not in case_cols
        assert "next_deadline_at" not in case_cols
        # Pre-existing row must survive the downgrade.
        with engine.connect() as conn:
            result = conn.execute(text("SELECT rol FROM cases WHERE rol='C-TEST-008'"))
            assert len(result.fetchall()) == 1

    def test_reupgrade_succeeds(self, sqlite_db) -> None:
        """upgrade → downgrade → upgrade must succeed without errors."""
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "008")
            command.downgrade(cfg, "007")
            command.upgrade(cfg, "008")
        assert "case_deadlines" in _tables(engine)

    def test_unique_constraint_enforced(self, sqlite_db) -> None:
        """UNIQUE(case_id, deadline_type, triggered_at) prevents duplicate rows."""
        import sqlalchemy.exc as sa_exc

        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "008")

        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO case_deadlines "
                "(case_id, deadline_type, due_date, triggered_at, status) "
                "VALUES (1, 'excepciones_8d', '2026-06-20', '2026-06-10', 'active')"
            ))
            conn.commit()

        with pytest.raises(sa_exc.IntegrityError):
            with engine.connect() as conn:
                conn.execute(text(
                    "INSERT INTO case_deadlines "
                    "(case_id, deadline_type, due_date, triggered_at, status) "
                    "VALUES (1, 'excepciones_8d', '2026-06-21', '2026-06-10', 'active')"
                ))
                conn.commit()
