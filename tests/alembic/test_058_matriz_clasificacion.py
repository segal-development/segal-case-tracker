"""Tests for migration 058: matriz de clasificación reference tables +
Case columns.

File-sanity checks (importable, revision ids, upgrade/downgrade callables)
and the single-head guard — the suite builds its schema from
``Base.metadata``, so the DDL itself is not exercised against a live DB here.
"""

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIGRATION_PATH = _REPO_ROOT / "alembic/versions/058_matriz_clasificacion.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_058", _MIGRATION_PATH)
    assert spec is not None, f"Migration file not found: {_MIGRATION_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration058Importable:
    def test_migration_file_exists(self):
        assert _MIGRATION_PATH.exists(), f"Expected migration at {_MIGRATION_PATH}"

    def test_upgrade_callable(self):
        assert callable(_load_migration().upgrade)

    def test_downgrade_callable(self):
        assert callable(_load_migration().downgrade)

    def test_revision_id(self):
        assert _load_migration().revision == "058"

    def test_down_revision_chains_to_057(self):
        assert _load_migration().down_revision == "057"


class TestMigrationChainHasSingleHead:
    def test_chain_has_a_single_head(self):
        """Linear chain — exactly ONE head, deliberately not pinned to a revision."""
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())
        assert len(heads) == 1, f"Expected a single migration head, got {heads}"


class TestModelsMatchMigration:
    def test_matriz_clasificacion_table_and_columns(self):
        from app.models.matriz_clasificacion import MatrizClasificacion

        assert MatrizClasificacion.__tablename__ == "matriz_clasificacion"
        cols = {c.name for c in MatrizClasificacion.__table__.columns}
        assert cols == {
            "id", "proc_simple", "proc_antiguo", "etapa", "etapa_padre",
            "orden", "condicion_rol", "matriz", "observaciones",
            "created_at", "updated_at",
        }

    def test_matriz_tramite_override_table_and_columns(self):
        from app.models.matriz_tramite_override import MatrizTramiteOverride

        assert MatrizTramiteOverride.__tablename__ == "matriz_tramite_override"
        cols = {c.name for c in MatrizTramiteOverride.__table__.columns}
        assert cols == {
            "id", "proc_antiguo", "etapa", "nombre_tramite", "matriz",
            "observaciones", "created_at", "updated_at",
        }

    def test_matriz_pjud_mapeo_table_and_columns(self):
        from app.models.matriz_pjud_mapeo import MatrizPjudMapeo

        assert MatrizPjudMapeo.__tablename__ == "matriz_pjud_mapeo"
        cols = {c.name for c in MatrizPjudMapeo.__table__.columns}
        assert cols == {
            "id", "pjud_stage", "matriz_etapa", "nota", "activo",
            "match_tipo", "orden", "created_at", "updated_at",
        }
        # Natural key is (pjud_stage, match_tipo), not pjud_stage alone.
        assert any(
            set(c.name for c in uc.columns) == {"pjud_stage", "match_tipo"}
            for uc in MatrizPjudMapeo.__table__.constraints
            if hasattr(uc, "columns")
        )

    def test_case_has_matriz_columns(self):
        from app.models.case import Case

        cols = {c.name for c in Case.__table__.columns}
        assert {
            "matriz", "matriz_etapa", "matriz_origen",
            "matriz_proc_simple", "matriz_computed_at",
        }.issubset(cols)
