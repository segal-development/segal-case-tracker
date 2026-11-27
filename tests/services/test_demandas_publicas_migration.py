"""Migration 094 (demandas públicas, búsqueda por fecha, public link tokens). No real DB."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_PATH = next((Path(__file__).parent.parent.parent / "alembic/versions").glob("094_*.py"), None)
TABLES = ("pjud_demandas_publicas", "pjud_busqueda_fecha_runs", "public_link_tokens")


def _load():
    spec = importlib.util.spec_from_file_location("migration_094", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _up(conn, mod):
    with Operations.context(MigrationContext.configure(conn)):
        mod.upgrade()


def test_migration_chain():
    mod = _load()
    assert mod.revision == "094" and mod.down_revision == "093"


def test_up_creates_three_tables_with_contract_columns():
    mod = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _up(conn, mod)
        insp = sa.inspect(conn)
        for t in TABLES:
            assert t in insp.get_table_names()
        cols = lambda t: {c["name"] for c in insp.get_columns(t)}  # noqa: E731
        assert cols("pjud_demandas_publicas") >= {
            "id", "rol", "tribunal", "court_id", "corte", "caratulado",
            "fecha_ingreso", "texto_demanda", "litigantes", "encontrada_at",
        }
        assert cols("pjud_busqueda_fecha_runs") >= {
            "id", "fecha", "corte", "status", "contadores", "started_at", "finished_at",
        }
        assert cols("public_link_tokens") >= {
            "id", "purpose", "token_hash", "created_at", "created_by", "revoked_at", "last_used_at",
        }


def test_up_enforces_unique_constraints():
    mod = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _up(conn, mod)
        ins = ("INSERT INTO pjud_demandas_publicas (rol, tribunal, corte, caratulado, litigantes, encontrada_at) "
               "VALUES ('C-1-2026','T','S','X','[]', '2026-01-01')")
        conn.execute(sa.text(ins))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(ins))
    with engine.begin() as conn:
        run = ("INSERT INTO pjud_busqueda_fecha_runs (fecha, corte, status, started_at) "
               "VALUES ('2026-01-01','S','running','2026-01-01')")
        conn.execute(sa.text(run))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(run))
    with engine.begin() as conn:
        tok = ("INSERT INTO public_link_tokens (purpose, token_hash, created_at) "
               "VALUES ('demandas_publicas','h','2026-01-01')")
        conn.execute(sa.text(tok))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(tok))


def test_down_drops_all_tables():
    mod = _load()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        _up(conn, mod)
        with Operations.context(MigrationContext.configure(conn)):
            mod.downgrade()
        names = sa.inspect(conn).get_table_names()
        assert not any(t in names for t in TABLES)
