"""
Pytest fixtures for PJUD HTML fixture files.

Provides helpers to load anonymized and synthetic detail HTML files
used by parser unit tests.
"""

import pathlib
import pytest

FIXTURES_DIR = pathlib.Path(__file__).parent


@pytest.fixture(scope="session")
def rich_html() -> str:
    """Anonymized civil case detail HTML.

    Contains:
    - 3 movements in #historiaCiv
    - 6 litigantes in #litigantesCiv (all personal data anonymized)
    - 0 notificaciones in #notificacionesCiv
    - 0 escritos in #escritosCiv
    - 1 exhorto in #exhortosCiv
    """
    return (FIXTURES_DIR / "detail_civil_rich.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def synthetic_html() -> str:
    """Synthetic detail HTML built on the anonymized rich fixture.

    Adds 2 notificacion rows and 2 escrito rows to the empty panes,
    so those parsers have test data despite no real populated sample
    being available.

    Contains everything in rich_html PLUS:
    - 2 notificaciones in #notificacionesCiv
    - 2 escritos in #escritosCiv (row 1 has a doc token, row 2 does not)
    """
    return (FIXTURES_DIR / "detail_civil_synthetic.html").read_text(encoding="utf-8")
