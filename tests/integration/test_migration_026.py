"""Integration round-trip test for migration 026 (Alert.read / read_at).

Strategy (mirrors tests/integration/test_migration_008.py):
 1. Build the minimal "025 state" schema directly with SQL (only the
    `alerts` table, which is all migration 026 touches).
 2. Stamp alembic_version at "025".
 3. Run command.upgrade(cfg, "026") ← exercises only OUR migration.
 4. Assert the new columns exist and existing rows backfill to read=False.
 5. Run command.downgrade(cfg, "025") ← exercises only OUR downgrade.
 6. Assert the rollback is clean (pre-existing data survives).
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

# Minimal DDL mirroring the "025 state" of the `alerts` table (all columns
# through migration 025; migration 026 only touches this one table).
_SCHEMA_AT_025 = """
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lawyer_id       INTEGER NOT NULL,
    case_id         INTEGER NOT NULL,
    movement_id     INTEGER,
    entity_type     TEXT,
    entity_id       INTEGER,
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    message         TEXT,
    email_sent      BOOLEAN DEFAULT 0,
    email_sent_at   TIMESTAMP,
    webhook_sent    BOOLEAN DEFAULT 0,
    webhook_sent_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num TEXT NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num) VALUES ('025');
"""


@pytest.fixture()
def sqlite_db(tmp_path):
    db_path = tmp_path / "test_migration_026.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        for stmt in _SCHEMA_AT_025.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
        conn.commit()
    yield engine, url
    engine.dispose()


@contextmanager
def _alembic_cfg(engine: Engine):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        yield cfg


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


@pytest.mark.integration
class TestMigration026Importable:
    def _load(self):
        path = ALEMBIC_DIR / "versions" / "026_add_alert_read_state.py"
        spec = importlib.util.spec_from_file_location("migration_026", path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_file_exists(self) -> None:
        assert (ALEMBIC_DIR / "versions" / "026_add_alert_read_state.py").exists()

    def test_revision_id(self) -> None:
        assert self._load().revision == "026"

    def test_down_revision_chains_to_025(self) -> None:
        assert self._load().down_revision == "025"


@pytest.mark.integration
class TestMigration026RoundTrip:
    def test_upgrade_adds_read_columns_and_backfills_existing_rows(self, sqlite_db) -> None:
        engine, _url = sqlite_db
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO alerts (lawyer_id, case_id, type, title) "
                "VALUES (1, 1, 'new_movement', 'Pre-existing alert')"
            ))
            conn.commit()

        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "026")

        cols = _columns(engine, "alerts")
        assert "read" in cols
        assert "read_at" in cols

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT read, read_at FROM alerts WHERE title='Pre-existing alert'"
            )).fetchone()
        assert row is not None
        assert row[0] in (0, False)
        assert row[1] is None

    def test_downgrade_drops_read_columns_and_preserves_data(self, sqlite_db) -> None:
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "026")
            with engine.connect() as conn:
                conn.execute(text(
                    "INSERT INTO alerts (lawyer_id, case_id, type, title) "
                    "VALUES (2, 2, 'new_movement', 'Survives downgrade')"
                ))
                conn.commit()
            command.downgrade(cfg, "025")

        cols = _columns(engine, "alerts")
        assert "read" not in cols
        assert "read_at" not in cols

        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT title FROM alerts WHERE title='Survives downgrade'"
            ))
            assert len(result.fetchall()) == 1

    def test_reupgrade_succeeds(self, sqlite_db) -> None:
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "026")
            command.downgrade(cfg, "025")
            command.upgrade(cfg, "026")
        assert "read" in _columns(engine, "alerts")
