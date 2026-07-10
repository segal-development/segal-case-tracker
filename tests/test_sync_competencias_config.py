"""Worker sync-competencias configuration tests.

The scheduled worker only syncs the competencias listed in SYNC_COMPETENCIAS
(CSV). The firm handles juicio-ejecutivo DEFENSE (civil) only, so the default
is civil-only; laboral/penal are opt-in via the env var without a code change.
"""

from app.config import Settings


def make_settings(**kwargs):
    """Instantiate Settings without reading the .env file."""
    return Settings(_env_file=None, **kwargs)


class TestSyncCompetenciasDefault:
    def test_default_is_civil_only(self):
        assert make_settings().sync_competencias_list == ["civil"]


class TestSyncCompetenciasParsing:
    def test_csv_parses_and_strips(self):
        settings = make_settings(SYNC_COMPETENCIAS="civil,laboral")
        assert settings.sync_competencias_list == ["civil", "laboral"]

    def test_whitespace_and_empties_dropped(self):
        settings = make_settings(SYNC_COMPETENCIAS=" civil , , penal ")
        assert settings.sync_competencias_list == ["civil", "penal"]
