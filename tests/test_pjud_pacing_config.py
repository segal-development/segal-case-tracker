"""Anti-Shape pacing configuration tests.

Verifies the token-bucket burst/rate defaults that stop PJUD detail/document
requests from firing in an instant, Shape-tripping burst, plus the new
inter-document delay and Shape cooldown settings.
"""

from app.config import Settings


def make_settings(**kwargs):
    """Instantiate Settings without reading the .env file."""
    return Settings(_env_file=None, **kwargs)


class TestBurstDefaults:
    def test_detail_burst_reduced_to_two(self):
        assert make_settings().PJUD_RL_DETAIL_BURST == 2

    def test_document_burst_reduced_to_one(self):
        assert make_settings().PJUD_RL_DOCUMENT_BURST == 1


class TestDocumentRate:
    def test_document_rate_slowed_down(self):
        assert make_settings().PJUD_RL_DOCUMENT_RATE == 0.15


class TestDocumentInterDelay:
    def test_document_inter_delay_default(self):
        assert make_settings().DOCUMENT_INTER_DELAY == 4.0


class TestShapeCooldownSettings:
    def test_base_seconds_default(self):
        assert make_settings().SHAPE_COOLDOWN_BASE_SECONDS == 300

    def test_max_seconds_default(self):
        assert make_settings().SHAPE_COOLDOWN_MAX_SECONDS == 900
