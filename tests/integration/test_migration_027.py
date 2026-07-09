"""Integration round-trip test for migration 027 (decision engine columns).

Strategy (mirrors tests/integration/test_migration_026.py):
 1. Build the minimal "026 state" schema directly with SQL (only the
    `cases` table, which is all migration 027 touches).
 2. Stamp alembic_version at "026".
 3. Run command.upgrade(cfg, "027") ← exercises only OUR migration.
 4. Assert the new columns exist and existing rows backfill to NULL.
 5. Run command.downgrade(cfg, "026") ← exercises only OUR downgrade.
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

# Minimal DDL mirroring the "026 state" of the `cases` table (just enough
# columns to insert a row and exercise the two columns migration 027 adds).
_SCHEMA_AT_026 = """
CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lawyer_id       INTEGER NOT NULL,
    court_id        INTEGER NOT NULL,
    rol             TEXT NOT NULL,
    competencia     TEXT DEFAULT 'civil',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num TEXT NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num) VALUES ('026');
"""


@pytest.fixture()
def sqlite_db(tmp_path):
    db_path = tmp_path / "test_migration_027.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        for stmt in _SCHEMA_AT_026.strip().split(";"):
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
class TestMigration027Importable:
    def _load(self):
        path = ALEMBIC_DIR / "versions" / "027_add_decision_engine_to_cases.py"
        spec = importlib.util.spec_from_file_location("migration_027", path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_file_exists(self) -> None:
        assert (ALEMBIC_DIR / "versions" / "027_add_decision_engine_to_cases.py").exists()

    def test_revision_id(self) -> None:
        assert self._load().revision == "027"

    def test_down_revision_chains_to_026(self) -> None:
        assert self._load().down_revision == "026"


@pytest.mark.integration
class TestMigration027RoundTrip:
    def test_upgrade_adds_decision_columns_and_backfills_existing_rows(self, sqlite_db) -> None:
        engine, _url = sqlite_db
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO cases (lawyer_id, court_id, rol) VALUES (1, 1, 'C-1-2026')"
            ))
            conn.commit()

        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "027")

        cols = _columns(engine, "cases")
        assert "recommended_action_code" in cols
        assert "next_review_at" in cols

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT recommended_action_code, next_review_at FROM cases WHERE rol='C-1-2026'"
            )).fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] is None

    def test_downgrade_drops_decision_columns_and_preserves_data(self, sqlite_db) -> None:
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "027")
            with engine.connect() as conn:
                conn.execute(text(
                    "INSERT INTO cases (lawyer_id, court_id, rol) VALUES (2, 2, 'C-2-2026')"
                ))
                conn.commit()
            command.downgrade(cfg, "026")

        cols = _columns(engine, "cases")
        assert "recommended_action_code" not in cols
        assert "next_review_at" not in cols

        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT rol FROM cases WHERE rol='C-2-2026'"
            ))
            assert len(result.fetchall()) == 1

    def test_reupgrade_succeeds(self, sqlite_db) -> None:
        engine, _url = sqlite_db
        with _alembic_cfg(engine) as cfg:
            command.upgrade(cfg, "027")
            command.downgrade(cfg, "026")
            command.upgrade(cfg, "027")
        assert "recommended_action_code" in _columns(engine, "cases")
