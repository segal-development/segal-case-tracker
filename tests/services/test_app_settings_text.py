"""Text settings (``app_text_settings``) and migration 093. No real DB."""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.services import app_settings

_PATH = Path(__file__).parent.parent.parent / "alembic/versions/093_app_text_settings.py"


def test_get_text_default_when_absent(db):
    assert app_settings.get_text(db, "k") is None
    assert app_settings.get_text(db, "k", "x") == "x"


def test_set_then_get_and_upsert(db):
    app_settings.set_text(db, "k", "a,b")
    db.commit()
    assert app_settings.get_text(db, "k") == "a,b"
    app_settings.set_text(db, "k", "")
    db.commit()
    assert app_settings.get_text(db, "k") == ""


def test_goals_int_settings_unchanged(db):
    app_settings.set_int(db, "n", 5)
    assert app_settings.get_int(db, "n", 0) == 5
    assert app_settings.get_text(db, "n") is None


def _load():
    spec = importlib.util.spec_from_file_location("migration_093", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_chain():
    mod = _load()
    assert mod.revision == "093" and mod.down_revision == "092"


def test_migration_up_and_down():
    mod = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
        insp = sa.inspect(conn)
        cols = {c["name"] for c in insp.get_columns("app_text_settings")}
        assert {"id", "key", "value", "updated_at"} <= cols
        conn.execute(sa.text("INSERT INTO app_text_settings (key, value) VALUES ('a','')"))
        with Operations.context(MigrationContext.configure(conn)):
            mod.downgrade()
        assert "app_text_settings" not in sa.inspect(conn).get_table_names()
