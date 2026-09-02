"""Tests for migration 055: cliente_sysgal_estados cache table.

File-sanity checks (importable, revision ids, upgrade/downgrade callables) and
the single-head guard — the suite builds its schema from ``Base.metadata``, so
the DDL itself is not exercised against a live DB here.
"""

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIGRATION_PATH = _REPO_ROOT / "alembic/versions/055_cliente_sysgal_estados.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_055", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration055Importable:
    def test_migration_file_exists(self):
        assert _MIGRATION_PATH.exists(), f"Expected migration at {_MIGRATION_PATH}"

    def test_upgrade_callable(self):
        assert callable(_load_migration().upgrade)

    def test_downgrade_callable(self):
        assert callable(_load_migration().downgrade)

    def test_revision_id(self):
        assert _load_migration().revision == "055"

    def test_down_revision_chains_to_054(self):
        assert _load_migration().down_revision == "054"


class TestMigrationChainHasSingleHead:
    def test_chain_has_a_single_head(self):
        """Linear chain — exactly ONE head, deliberately not pinned to a revision."""
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())
        assert len(heads) == 1, f"Expected a single migration head, got {heads}"


class TestModelMatchesMigration:
    def test_model_table_name_and_columns(self):
        from app.models.cliente_sysgal_estado import ClienteSysgalEstado

        assert ClienteSysgalEstado.__tablename__ == "cliente_sysgal_estados"
        cols = {c.name for c in ClienteSysgalEstado.__table__.columns}
        assert cols == {
            "id",
            "rut",
            "encontrado",
            "estado_codigo",
            "estado_label",
            "tiene_contrato",
            "vigencia_hasta",
            "sysgal_updated_at",
            "synced_at",
        }
        rut_col = ClienteSysgalEstado.__table__.c.rut
        assert rut_col.unique is True or any(
            idx.unique and [c.name for c in idx.columns] == ["rut"]
            for idx in ClienteSysgalEstado.__table__.indexes
        )
